import Foundation

/// An email address said out loud, as the transcript writes it ("N-I-R K A B E S S A at gmail dot
/// com", "sam dot lee at example dot org"), shown as the address ("nirkabessa@gmail.com"). Display
/// only: the words the voice and Hermes received are unchanged (SPK-304).
///
/// The local part is a run of spelled letters/digits ("K A B", "N-I-R"), or one ordinary word that
/// isn't a common verb/pronoun ("look at google dot com" stays as said); parts may be joined by
/// spoken "dot", "underscore", "dash" or "hyphen". The domain ends in a known top-level domain,
/// spoken ("dot com") or written (".com").
public enum SpokenEmail {
    static let tlds = ["com", "org", "net", "io", "co", "ai", "edu", "gov", "me", "dev", "app", "us", "uk",
                       "ca", "de", "fr", "info", "biz", "xyz", "email", "mail"]
    /// Words that, before "at", are the sentence, not a mailbox ("I work at …", "look at …").
    static let notMailbox: Set<String> = Set("""
    a an the i me you we us they them him her it is am are was were be been i'm im to of in on for from with by \
    and or but so that this there here then what where when who look looking looked work works working worked \
    stay staying live living lives arrive arrived arriving meet meeting see email emailed mail message messaged \
    reach reached write wrote send sent text texted call called site page link go went get got just also still \
    home office back up out over down now one two all any some
    """.split(whereSeparator: { $0.isWhitespace }).map(String.init))

    private static let spelled = #"[A-Za-z0-9](?:-[A-Za-z0-9])*\.?"#
    private static let word = #"[A-Za-z0-9][A-Za-z0-9_-]*"#
    private static let joiner = #"(?:dot|underscore|dash|hyphen|period)"#
    private static let part = "(?:\(spelled)(?:[ ]+\(spelled))+|\(spelled)|\(word))"
    private static let local = "\(part)(?:[ ]+\(joiner)[ ]+\(part))*"
    private static let dotSep = #"(?:[ ]+dot[ ]+|\.)"#
    private static let label = #"[A-Za-z0-9][A-Za-z0-9-]*"#
    private static let pattern = #"(?i)(?<![A-Za-z0-9@._'\u2019-])("# + local + #")[ ]+at[ ]+("# + label
        + "(?:\(dotSep)\(label))*)\(dotSep)(\(tlds.joined(separator: "|")))(?![A-Za-z0-9])"
    private static let regex = try! NSRegularExpression(pattern: pattern)

    /// *text* with every spoken address written as one.
    public static func collapsed(_ text: String) -> String {
        guard text.range(of: " at ", options: .caseInsensitive) != nil else { return text }
        let ns = text as NSString
        var out = text
        for m in regex.matches(in: text, range: NSRange(location: 0, length: ns.length)).reversed() {
            var localSaid = ns.substring(with: m.range(at: 1))
            var range = m.range
            // "send a K A B at …": a lowercase letter before capitals is the sentence's ("a"), not the name's.
            if localSaid.count > 2, localSaid.first?.isLowercase == true, localSaid.dropFirst().first == " ",
               localSaid.dropFirst(2).contains(where: \.isUppercase) {
                localSaid = String(localSaid.dropFirst(2))
                range = NSRange(location: range.location + 2, length: range.length - 2)
            }
            guard let mailbox = mailbox(localSaid) else { continue }
            let domain = ns.substring(with: m.range(at: 2))
                .replacingOccurrences(of: #"(?i)[ ]+dot[ ]+"#, with: ".", options: .regularExpression)
            let address = "\(mailbox)@\(domain).\(ns.substring(with: m.range(at: 3)))".lowercased()
            out = (out as NSString).replacingCharacters(in: range, with: address)
        }
        return out
    }

    /// The mailbox name the words spell, or nil when they read as part of the sentence.
    static func mailbox(_ said: String) -> String? {
        let tokens = said.split(separator: " ").map(String.init)
        var out = "", spelledLetters = 0, words: [String] = []
        for t in tokens {
            switch t.lowercased() {
            case "dot", "period": out += "."
            case "underscore": out += "_"
            case "dash", "hyphen": out += "-"
            default:
                let bare = t.replacingOccurrences(of: "-", with: "").replacingOccurrences(of: ".", with: "")
                if t.range(of: "^" + spelled + "$", options: .regularExpression) != nil, bare.count <= 1 || t.contains("-") {
                    spelledLetters += bare.count
                } else {
                    words.append(t.lowercased())
                }
                out += bare
            }
        }
        if out.isEmpty || out.hasPrefix(".") || out.hasSuffix(".") { return nil }
        // Spelled out letter by letter: clearly an address.
        if spelledLetters >= 2 && words.isEmpty { return out }
        // Ordinary words: only when none of them reads as the sentence around it.
        if words.contains(where: { notMailbox.contains($0) }) { return nil }
        if tokens.count == 1, notMailbox.contains(out.lowercased()) { return nil }
        return out
    }
}
