import Foundation

// Pure pieces of the relay call transport (Grok Voice): the plugin owns the provider socket and
// relays raw PCM over one WebSocket. Audio hardware lives in SpeakeasyClient/RelayCallEngine; what
// can be tested without it lives here.

/// How the call's audio travels, from `transport` in the `POST /voice/sessions` answer.
public enum SessionTransport: Equatable, Sendable {
    /// WebRTC straight to the provider: the answer SDP.
    case webrtc(sdp: String)
    /// Audio relayed through the plugin over a WebSocket at `path` on the paired server.
    case relay(RelayTransport)
}

public struct RelayTransport: Equatable, Sendable {
    public var path: String
    public var encoding: String
    public var rate: Int
    public var channels: Int

    public init(path: String, encoding: String = "pcm16le", rate: Int = 24_000, channels: Int = 1) {
        self.path = path; self.encoding = encoding; self.rate = rate; self.channels = channels
    }

    /// `{"type":"relay","path":"/voice/interactions/<id>/audio","format":{...}}`. The path must be a
    /// server-relative path: the bearer is sent with it, so it never names another host.
    public init?(json: Any?) {
        guard let object = json as? [String: Any], (object["type"] as? String) == "relay",
              let path = object["path"] as? String, path.hasPrefix("/"), !path.hasPrefix("//"),
              !path.contains("://"), !path.contains("..") else { return nil }
        let format = object["format"] as? [String: Any]
        let encoding = format?["encoding"] as? String ?? "pcm16le"
        let rate = (format?["rate"] as? NSNumber)?.intValue ?? 24_000
        let channels = (format?["channels"] as? NSNumber)?.intValue ?? 1
        // The engine speaks exactly one format; anything else is a contract change, not a guess.
        guard encoding == "pcm16le", rate == 24_000, channels == 1 else { return nil }
        self.init(path: path, encoding: encoding, rate: rate, channels: channels)
    }

    /// The WebSocket URL on the paired server: http→ws, https→wss, same host and port.
    public func socketURL(base: URL) -> URL? {
        guard var components = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        switch components.scheme?.lowercased() {
        case "http": components.scheme = "ws"
        case "https": components.scheme = "wss"
        case "ws", "wss": break
        default: return nil
        }
        var prefix = components.path
        while prefix.hasSuffix("/") { prefix.removeLast() }
        components.path = prefix + path
        components.query = nil; components.fragment = nil
        return components.url
    }
}

/// Text control frames on the relay socket.
public enum RelayControl: Equatable, Sendable {
    case speechStarted
    case responseDone
    case closed(reason: String?)
    /// Anything else with a `type` (forwarded to the data-channel parser: transcripts, errors).
    case other(String)

    public init?(text: String) {
        guard let object = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let type = object["type"] as? String else { return nil }
        switch type {
        case "speech_started": self = .speechStarted
        case "response_done": self = .responseDone
        case "closed": self = .closed(reason: object["reason"] as? String)
        default: self = .other(type)
        }
    }

    /// App→plugin frames.
    public static func mute(_ muted: Bool) -> String { muted ? #"{"type":"mute","muted":true}"# : #"{"type":"mute","muted":false}"# }
    public static let end = #"{"type":"end"}"#
}

/// PCM16LE 24 kHz mono helpers for the relay.
public enum RelayPCM {
    public static let sampleRate = 24_000
    /// 40 ms at 24 kHz mono 16-bit = 960 samples = 1920 bytes.
    public static let chunkBytes = 1_920

    /// Float samples (-1...1) to PCM16LE, clipping.
    public static func pcm16(_ samples: UnsafeBufferPointer<Float>) -> Data {
        var output = Data(count: samples.count * 2)
        output.withUnsafeMutableBytes { raw in
            let out = raw.bindMemory(to: Int16.self)
            for i in 0..<samples.count {
                let clipped = max(-1, min(1, samples[i]))
                out[i] = Int16(clipped * Float(Int16.max)).littleEndian
            }
        }
        return output
    }

    /// PCM16LE to floats for playback. A trailing odd byte is ignored.
    public static func floats(_ data: Data) -> [Float] {
        let count = data.count / 2
        var out = [Float](repeating: 0, count: count)
        data.withUnsafeBytes { raw in
            for i in 0..<count {
                let lo = UInt16(raw[2 * i]), hi = UInt16(raw[2 * i + 1])
                out[i] = Float(Int16(bitPattern: lo | (hi << 8))) / 32_768
            }
        }
        return out
    }

    /// Root-mean-square level (0...1) of PCM16LE audio.
    public static func level(_ data: Data) -> Double {
        let samples = floats(data)
        guard !samples.isEmpty else { return 0 }
        let sum = samples.reduce(0) { $0 + Double($1) * Double($1) }
        return min(1, sqrt(sum / Double(samples.count)))
    }
}

/// Cuts a stream of PCM bytes into fixed-size frames (~40 ms) so the plugin gets steady chunks
/// whatever buffer size the audio hardware delivers.
public struct PCMChunker: Sendable {
    public let chunkBytes: Int
    private var pending = Data()

    public init(chunkBytes: Int = RelayPCM.chunkBytes) {
        precondition(chunkBytes > 0 && chunkBytes % 2 == 0)
        self.chunkBytes = chunkBytes
    }

    public var pendingBytes: Int { pending.count }

    /// Adds bytes; returns every whole chunk now available (the rest waits for more).
    public mutating func append(_ data: Data) -> [Data] {
        pending.append(data)
        var chunks: [Data] = []
        var start = pending.startIndex
        while pending.endIndex - start >= chunkBytes {
            chunks.append(Data(pending[start..<(start + chunkBytes)]))
            start += chunkBytes
        }
        if start != pending.startIndex { pending = Data(pending[start...]) }
        return chunks
    }

    /// Drops a partial chunk (mute, or the call ends).
    public mutating func reset() { pending = Data() }
}

/// Bookkeeping for queued assistant audio, without the player. Each scheduled buffer is tagged
/// with the generation it was queued in; a barge-in (`speech_started`) flushes: the generation
/// moves on, the queue empties, and completions from flushed buffers no longer count.
public struct RelayPlaybackQueue: Sendable {
    public private(set) var generation = 0
    public private(set) var queuedBuffers = 0
    public private(set) var queuedSamples = 0
    public private(set) var flushes = 0
    /// Samples thrown away by flushes (assistant audio cut off by the user talking).
    public private(set) var droppedSamples = 0

    public init() {}

    public var isPlaying: Bool { queuedBuffers > 0 }

    /// Records a buffer about to be scheduled; returns its generation tag.
    public mutating func enqueue(samples: Int) -> Int {
        queuedBuffers += 1
        queuedSamples += samples
        return generation
    }

    /// A buffer finished playing. Stale tags (flushed) are ignored.
    public mutating func finished(samples: Int, generation tag: Int) {
        guard tag == generation, queuedBuffers > 0 else { return }
        queuedBuffers -= 1
        queuedSamples = max(0, queuedSamples - samples)
    }

    /// Barge-in. Returns true when there was audio to stop (the player must be stopped).
    @discardableResult
    public mutating func flush() -> Bool {
        let had = queuedBuffers > 0
        generation += 1
        flushes += 1
        droppedSamples += queuedSamples
        queuedBuffers = 0
        queuedSamples = 0
        return had
    }
}
