import AVFoundation
import Foundation
import SpeakeasyCore

/// Relay transport (Grok Voice): the plugin owns the provider socket; this engine streams the mic as
/// PCM16LE 24 kHz mono over a WebSocket to the paired server and plays the PCM that comes back.
///
/// There is no WebRTC here, so echo cancellation comes from Apple's voice processing on the input
/// node. Barge-in: `speech_started` from the plugin flushes whatever assistant audio is queued.
/// All public callbacks are delivered on the main queue.
public final class RelayCallEngine: NSObject, CallTransport, URLSessionWebSocketDelegate, @unchecked Sendable {
    public struct Options: Sendable {
        /// Feed a generated tone through the capture path instead of the microphone (smoke tests on
        /// a Mac without a mic). Voice processing is skipped then.
        public var syntheticMic: Bool
        /// Player volume (the smoke runs silently with 0).
        public var playbackVolume: Float
        public init(syntheticMic: Bool = false, playbackVolume: Float = 1) {
            self.syntheticMic = syntheticMic; self.playbackVolume = playbackVolume
        }
    }

    /// Counters for diagnostics and the loopback smoke.
    public struct Stats: Sendable {
        public var bytesSent = 0
        public var framesSent = 0
        public var bytesReceived = 0
        public var framesReceived = 0
        public var controlSent: [String] = []
        public var controlReceived: [String] = []
        public var flushes = 0
        public var flushesThatStoppedAudio = 0
        public var droppedSamples = 0
        public var opened = false
        public var closedReason: String?
        public var voiceProcessing = false
        public var maxVoiceLevel = 0.0
    }

    public enum EngineError: LocalizedError {
        case noInput, badURL
        public var errorDescription: String? {
            switch self {
            case .noInput: return "No microphone input is available"
            case .badURL: return "The server's audio address isn't usable"
            }
        }
    }

    public var onMessage: ((Data) -> Void)?
    public var onChannelOpen: (() -> Void)?
    public var onChannelClosed: (() -> Void)?
    public var onConnectionFailed: (() -> Void)?
    public var onAudioFormatChanged: (() -> Void)?

    private let options: Options
    private let lock = NSLock()
    // Guarded by `lock`.
    private var micEnabled = true
    private var muteSent = false
    private var socketOpen = false
    private var closed = false
    private var endNotified = false
    private var held = false
    private var wantsAudio = false
    private var audioRunning = false
    private var remoteEnabled = true
    private var chunker = PCMChunker()
    private var queue = RelayPlaybackQueue()
    private var micLevel = 0.0
    private var voiceLevel = 0.0
    private var stats = Stats()

    private let io = DispatchQueue(label: "speakeasy.relay.io")
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private let audio = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let playFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Double(RelayPCM.sampleRate),
                                           channels: 1, interleaved: false)!
    private let sendFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(RelayPCM.sampleRate),
                                           channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private var converterSource: AVAudioFormat?
    private var synthTimer: DispatchSourceTimer?
    private var synthPhase = 0.0
    private var configObserver: NSObjectProtocol?
    private var interruptionObserver: NSObjectProtocol?

    public init(options: Options = Options()) {
        self.options = options
        super.init()
    }

    public var currentStats: Stats { lock.lock(); defer { lock.unlock() }; var s = stats; s.flushes = queue.flushes; s.droppedSamples = queue.droppedSamples; return s }

    // MARK: Start

    /// Opens the socket (`request` carries the bearer) and, unless the listener holds the mic,
    /// the audio. Audio captured before the socket opens is dropped.
    public func start(request: URLRequest) throws {
        guard let scheme = request.url?.scheme?.lowercased(), scheme == "ws" || scheme == "wss" else { throw EngineError.badURL }
        lock.lock(); wantsAudio = true; let hold = held; lock.unlock()
        if !hold { try startAudio() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        let delegateQueue = OperationQueue()
        delegateQueue.maxConcurrentOperationCount = 1
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: delegateQueue)
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 4 << 20
        self.session = session
        self.socket = socket
        socket.resume()
        receive()
    }

    private func startAudio() throws {
        lock.lock()
        guard !audioRunning, !closed else { lock.unlock(); return }
        lock.unlock()
        #if os(iOS)
        let s = AVAudioSession.sharedInstance()
        var sessionOptions: AVAudioSession.CategoryOptions = [.defaultToSpeaker]
        if #available(iOS 26.0, *) { sessionOptions.insert(.allowBluetoothHFP) } else { sessionOptions.insert(.allowBluetooth) }
        try s.setCategory(.playAndRecord, mode: .voiceChat, options: sessionOptions)
        try s.setActive(true)
        #endif
        var processing = false
        if !options.syntheticMic {
            let input = audio.inputNode
            // Echo cancellation: there is no WebRTC AEC on this path. Must be on before the engine starts.
            do { try input.setVoiceProcessingEnabled(true); processing = true } catch {
                NSLog("Speakeasy relay: voice processing unavailable: \(error.localizedDescription)")
            }
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else { throw EngineError.noInput }
            input.installTap(onBus: 0, bufferSize: 2_048, format: format) { [weak self] buffer, _ in self?.captured(buffer) }
        }
        audio.attach(player)
        audio.connect(player, to: audio.mainMixerNode, format: playFormat)
        player.installTap(onBus: 0, bufferSize: 1_024, format: playFormat) { [weak self] buffer, _ in self?.played(buffer) }
        audio.prepare()
        try audio.start()
        player.play()
        lock.lock()
        audioRunning = true
        stats.voiceProcessing = processing
        player.volume = remoteEnabled ? options.playbackVolume : 0
        lock.unlock()
        configObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: audio,
                                                                queue: .main) { [weak self] _ in self?.configurationChanged() }
        #if os(iOS)
        if interruptionObserver == nil {
            interruptionObserver = NotificationCenter.default.addObserver(
                forName: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(),
                queue: .main) { [weak self] note in
                    self?.interrupted(typeRaw: note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                                      optionsRaw: note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt)
                }
        }
        #endif
        if options.syntheticMic { startSyntheticMic() }
    }

    #if os(iOS)
    /// A phone call or Siri stopped the engine; when it ends, take the session back and restart it.
    private func interrupted(typeRaw: UInt?, optionsRaw: UInt?) {
        lock.lock(); let running = audioRunning, isClosed = closed; lock.unlock()
        guard AudioInterruption.action(typeRaw: typeRaw, optionsRaw: optionsRaw,
                                       audioRunning: running, closed: isClosed) == .restart else { return }
        do {
            try AVAudioSession.sharedInstance().setActive(true)
            if !audio.isRunning { try audio.start() }
            player.play()
        } catch {
            NSLog("Speakeasy relay: audio didn't come back after an interruption: \(error.localizedDescription)")
        }
    }
    #endif

    private func configurationChanged() {
        lock.lock(); let active = audioRunning && !closed; lock.unlock()
        guard active else { return }
        #if os(macOS)
        // The Mac's device changed under the engine: the client reopens the call on the new device.
        onAudioFormatChanged?()
        #else
        // iOS: the session moves the route itself; restart the engine on it.
        try? audio.start()
        player.play()
        #endif
    }

    // MARK: Capture

    private func captured(_ buffer: AVAudioPCMBuffer) {
        guard let mono = monoFloat(buffer) else { return }
        let level = rms(mono)
        lock.lock()
        micLevel = level
        let sending = socketOpen && micEnabled && !held && !closed
        lock.unlock()
        guard sending, let pcm = convert(mono) else { return }
        lock.lock()
        let chunks = chunker.append(pcm)
        lock.unlock()
        for chunk in chunks { sendBinary(chunk) }
    }

    /// Channel 0 as a mono float buffer (voice processing on the Mac can deliver several channels).
    private func monoFloat(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let format = buffer.format
        if format.commonFormat == .pcmFormatFloat32, format.channelCount == 1, !format.isInterleaved { return buffer }
        guard let source = buffer.floatChannelData, !format.isInterleaved,
              let monoFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate, channels: 1, interleaved: false),
              let mono = AVAudioPCMBuffer(pcmFormat: monoFormat, frameCapacity: buffer.frameLength),
              let target = mono.floatChannelData else { return nil }
        mono.frameLength = buffer.frameLength
        target[0].update(from: source[0], count: Int(buffer.frameLength))
        return mono
    }

    /// Resample + quantise to PCM16LE 24 kHz mono.
    private func convert(_ mono: AVAudioPCMBuffer) -> Data? {
        if converter == nil || converterSource != mono.format {
            converter = AVAudioConverter(from: mono.format, to: sendFormat)
            converterSource = mono.format
        }
        guard let converter else { return nil }
        let ratio = sendFormat.sampleRate / mono.format.sampleRate
        let capacity = AVAudioFrameCount((Double(mono.frameLength) * ratio).rounded(.up)) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: sendFormat, frameCapacity: capacity) else { return nil }
        var given = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return mono
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData else { return nil }
        // Apple platforms are little-endian: the Int16 bytes are already PCM16LE.
        return Data(bytes: samples[0], count: Int(out.frameLength) * 2)
    }

    private func rms(_ buffer: AVAudioPCMBuffer) -> Double {
        guard let data = buffer.floatChannelData, buffer.frameLength > 0 else { return 0 }
        let n = Int(buffer.frameLength)
        var sum: Double = 0
        for i in 0..<n { let v = Double(data[0][i]); sum += v * v }
        return min(1, (sum / Double(n)).squareRoot())
    }

    /// 20 ms of a quiet 440 Hz tone at 48 kHz, through the same convert/chunk path as the mic.
    private func startSyntheticMic() {
        let timer = DispatchSource.makeTimerSource(queue: io)
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
        timer.schedule(deadline: .now(), repeating: .milliseconds(20))
        timer.setEventHandler { [weak self] in
            guard let self, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 960),
                  let data = buffer.floatChannelData else { return }
            buffer.frameLength = 960
            for i in 0..<960 {
                data[0][i] = Float(sin(self.synthPhase) * 0.3)
                self.synthPhase += 2 * .pi * 440 / 48_000
            }
            self.captured(buffer)
        }
        synthTimer = timer
        timer.resume()
    }

    // MARK: Playback

    private func play(_ data: Data) {
        let samples = RelayPCM.floats(data)
        lock.lock()
        stats.bytesReceived += data.count
        stats.framesReceived += 1
        let running = audioRunning && !closed
        lock.unlock()
        guard running, !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData else { return }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { channel[0].update(from: $0.baseAddress!, count: samples.count) }
        lock.lock()
        let tag = queue.enqueue(samples: samples.count)
        lock.unlock()
        let count = samples.count
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.lock.lock(); self.queue.finished(samples: count, generation: tag); self.lock.unlock()
        }
    }

    private func played(_ buffer: AVAudioPCMBuffer) {
        let level = rms(buffer)
        lock.lock()
        voiceLevel = queue.isPlaying ? level : 0
        stats.maxVoiceLevel = max(stats.maxVoiceLevel, voiceLevel)
        lock.unlock()
    }

    /// Barge-in: drop queued assistant audio at once.
    private func flushPlayback() {
        lock.lock()
        let had = queue.flush()
        if had { stats.flushesThatStoppedAudio += 1 }
        voiceLevel = 0
        let running = audioRunning
        lock.unlock()
        // Outside the lock: stop() fires the flushed buffers' completions, which take it.
        if had && running {
            player.stop()
            player.play()
        }
    }

    // MARK: Socket

    private func receive() {
        socket?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(.data(let data)):
                self.play(data)
            case .success(.string(let text)):
                self.control(text)
            case .success:
                break
            case .failure:
                self.socketEnded(reason: nil)
                return
            }
            self.lock.lock(); let done = self.closed || self.endNotified; self.lock.unlock()
            if !done { self.receive() }
        }
    }

    private func control(_ text: String) {
        guard let message = RelayControl(text: text) else { return }
        lock.lock(); stats.controlReceived.append(text); lock.unlock()
        switch message {
        case .speechStarted: flushPlayback()
        case .responseDone: break
        case .closed(let reason): socketEnded(reason: reason ?? "closed")
        case .other:
            // Transcripts, errors, session events in the data-channel shape.
            let data = Data(text.utf8)
            DispatchQueue.main.async { [weak self] in self?.onMessage?(data) }
        }
    }

    private func sendBinary(_ data: Data) {
        io.async { [weak self] in
            guard let self, let socket = self.socket else { return }
            self.lock.lock()
            guard self.socketOpen, !self.closed else { self.lock.unlock(); return }
            self.stats.bytesSent += data.count
            self.stats.framesSent += 1
            self.lock.unlock()
            socket.send(.data(data)) { _ in }
        }
    }

    private func sendText(_ text: String) {
        io.async { [weak self] in
            guard let self, let socket = self.socket else { return }
            self.lock.lock(); self.stats.controlSent.append(text); self.lock.unlock()
            socket.send(.string(text)) { _ in }
        }
    }

    private func socketEnded(reason: String?) {
        lock.lock()
        guard !closed, !endNotified else { lock.unlock(); return }
        endNotified = true
        socketOpen = false
        if stats.closedReason == nil { stats.closedReason = reason ?? "socket closed" }
        lock.unlock()
        DispatchQueue.main.async { [weak self] in self?.onChannelClosed?() }
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        lock.lock()
        socketOpen = true
        stats.opened = true
        let muted = !micEnabled
        if muted { muteSent = true }
        lock.unlock()
        if muted { sendText(RelayControl.mute(true)) }
        // The relay is the session: the call is live once audio can flow.
        let started = Data(#"{"type":"session.started"}"#.utf8)
        DispatchQueue.main.async { [weak self] in
            self?.onMessage?(started)
            self?.onChannelOpen?()
        }
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                           didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        socketEnded(reason: reason.flatMap { String(data: $0, encoding: .utf8) } ?? "close code \(closeCode.rawValue)")
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        socketEnded(reason: error?.localizedDescription)
    }

    // MARK: CallTransport

    @discardableResult
    public func send(json object: [String: Any]) -> Bool {
        lock.lock(); let open = socketOpen && !closed; lock.unlock()
        guard open else { return false }
        if (object["type"] as? String) == "session.close" { sendText(RelayControl.end); return true }
        return false
    }

    public func setMicEnabled(_ enabled: Bool) {
        lock.lock()
        micEnabled = enabled
        let notify = socketOpen && !closed && muteSent == enabled
        if notify { muteSent = !enabled }
        if !enabled { chunker.reset() }
        lock.unlock()
        if notify { sendText(RelayControl.mute(!enabled)) }
    }

    public func setRemoteAudioEnabled(_ enabled: Bool) {
        lock.lock(); remoteEnabled = enabled; let running = audioRunning; lock.unlock()
        if running { player.volume = enabled ? options.playbackVolume : 0 }
    }

    /// While the assistant's audio plays, the mic level reads at least the voice level: the orb's
    /// "speaking" state comes from transcripts, which a relay may not send.
    public func audioLevels(_ completion: @escaping (_ mic: Double, _ voice: Double) -> Void) {
        lock.lock()
        let voice = voiceLevel
        let mic = queue.isPlaying ? max(micLevel, voice) : micLevel
        lock.unlock()
        DispatchQueue.main.async { completion(mic, voice) }
    }

    public func micProbe(_ completion: @escaping (_ level: Double?, _ packetsSent: Int?) -> Void) {
        lock.lock()
        let live = audioRunning && !held && !closed
        let level = live ? micLevel : nil
        let frames = live ? stats.framesSent : nil
        lock.unlock()
        DispatchQueue.main.async { completion(level, frames) }
    }

    /// iPhone: the on-device listener has the mic; the engine's audio waits for `releaseAudio()`.
    /// The Mac shares the input, as with WebRTC.
    public func holdAudio() {
        #if os(iOS) || os(visionOS)
        lock.lock(); held = true; lock.unlock()
        #endif
    }

    public func releaseAudio() {
        lock.lock()
        guard held else { lock.unlock(); return }
        held = false
        let start = wantsAudio && !audioRunning && !closed
        lock.unlock()
        if start {
            do { try startAudio() } catch {
                NSLog("Speakeasy relay: audio didn't start: \(error.localizedDescription)")
                DispatchQueue.main.async { [weak self] in self?.onConnectionFailed?() }
            }
        }
    }

    public var isAudioHeld: Bool { lock.lock(); defer { lock.unlock() }; return held }

    public func close() {
        lock.lock()
        guard !closed else { lock.unlock(); return }
        closed = true
        held = false
        socketOpen = false
        let running = audioRunning
        audioRunning = false
        chunker.reset()
        lock.unlock()
        synthTimer?.cancel(); synthTimer = nil
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        if let interruptionObserver { NotificationCenter.default.removeObserver(interruptionObserver) }
        interruptionObserver = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        session?.invalidateAndCancel()
        if running {
            if !options.syntheticMic { audio.inputNode.removeTap(onBus: 0) }
            player.removeTap(onBus: 0)
            player.stop()
            audio.stop()
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            #endif
        }
    }
}
