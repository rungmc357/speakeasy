import AppKit

/// The Speakeasy mark (a keyhole with a soundwave cut into its round top), drawn in code so the
/// menu bar gets a crisp template image at any scale. Shapes match brand/build.py.
enum BrandGlyph {
    /// Menu-bar template image; `badge` adds a dot for "something finished while you were away";
    /// `listening` adds a ring at the bottom right while listening mode holds the mic.
    static func menuBarImage(badge: Bool = false, listening: Bool = false, height: CGFloat = 16) -> NSImage {
        let box = CGRect(x: 156, y: 86, width: 200, height: 338)   // the mark's bounds in its 512 design space
        let scale = height / box.height
        let width = (box.width * scale).rounded(.up) + (badge || listening ? 6 : 0)
        let image = NSImage(size: NSSize(width: width, height: height), flipped: true) { _ in
            let t = NSAffineTransform()
            t.scale(by: scale)
            t.translateX(by: -box.minX, yBy: -box.minY)
            let path = mark()
            path.transform(using: t as AffineTransform)
            NSColor.black.setFill()
            path.fill()
            if badge {
                NSBezierPath(ovalIn: NSRect(x: width - 5, y: 0, width: 5, height: 5)).fill()
            }
            if listening {
                let ring = NSBezierPath(ovalIn: NSRect(x: width - 5.5, y: height - 6, width: 5, height: 5))
                ring.lineWidth = 1.4
                NSColor.black.setStroke()
                ring.stroke()
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = ["Speakeasy", badge ? "work finished" : nil, listening ? "listening mode on" : nil]
            .compactMap { $0 }.joined(separator: ", ")
        return image
    }

    /// Keyhole silhouette with the five bars as holes (even-odd), in the 512 design space (y down).
    static func mark() -> NSBezierPath {
        let path = NSBezierPath()
        path.windingRule = .evenOdd
        path.move(to: NSPoint(x: 214, y: 270.9))
        // Round top: from the left of the neck, over the top, to the right of the neck.
        path.appendArc(withCenter: NSPoint(x: 256, y: 196), radius: 100, startAngle: 119.3, endAngle: 60.7 + 360, clockwise: false)
        path.line(to: NSPoint(x: 332, y: 414))
        path.line(to: NSPoint(x: 180, y: 414))
        path.close()
        let heights: [CGFloat] = [34, 70, 104, 70, 34]
        let w: CGFloat = 18, gap: CGFloat = 13, cy: CGFloat = 186
        var x = 256 - (CGFloat(heights.count) * w + CGFloat(heights.count - 1) * gap) / 2
        for h in heights {
            path.append(NSBezierPath(roundedRect: NSRect(x: x, y: cy - h / 2, width: w, height: h), xRadius: w / 2, yRadius: w / 2))
            x += w + gap
        }
        return path
    }
}
