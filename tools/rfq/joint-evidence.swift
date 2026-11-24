import AppKit
import PDFKit

// Read precisely the bytes that the authenticated original link will return.
struct Passage: Encodable {
    let text: String
    let bounds: [Double]
}

enum EvidenceError: Error { case unreadable, missingPassage, invalidBounds }

let data = FileHandle.standardInput.readDataToEndOfFile()
guard let document = PDFDocument(data: data), let page = document.page(at: 0),
      let text = page.string else { throw EvidenceError.unreadable }
let box = page.bounds(for: .cropBox)
let full = NSRange(location: 0, length: (text as NSString).length)

func match(_ pattern: String, in range: NSRange, group: Int = 0) throws -> NSRange {
    let regex = try NSRegularExpression(pattern: pattern)
    guard let found = regex.firstMatch(in: text, range: range) else { throw EvidenceError.missingPassage }
    return found.range(at: group)
}

func passage(_ range: NSRange) throws -> Passage {
    guard let selection = page.selection(for: range) else { throw EvidenceError.missingPassage }
    let rect = selection.bounds(for: page).intersection(box)
    guard rect.width > 0, rect.height > 0 else { throw EvidenceError.invalidBounds }
    return Passage(text: (text as NSString).substring(with: range),
                   bounds: [(rect.minX - box.minX) / box.width, (box.maxY - rect.maxY) / box.height,
                            rect.width / box.width, rect.height / box.height])
}

var passages = [Passage]()
switch CommandLine.arguments.last {
case "offer":
    // Limit the labels to item 1, between its numbered heading and item 2.
    let item = try match(#"(?ms)^\d+\. SCOPE OF SUPPLY:.*?^1[ \t]+\d+[ \t]+(.*?)(?=^2[ \t]+\d+[ \t]+)"#, in: full, group: 1)
    let fields = try NSRegularExpression(pattern: #"(?m)^[^:\r\n]+:[ \t]*[^\r\n]+"#)
    for field in fields.matches(in: text, range: item) {
        passages.append(try passage(field.range))
    }
case "datasheet":
    // The first equipment row carries a multiline flow and minimum-pressure condition.
    passages.append(try passage(match(#"(?m)^[\d.]+\s+m[3³]/h[^\n]*\nRequired at Slurry Pump Gland\s*\n=\s*[^\n]+"#, in: full)))
default:
    throw EvidenceError.missingPassage
}

let image = page.thumbnail(of: NSSize(width: box.width * 2, height: box.height * 2), for: .cropBox)
guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else { throw EvidenceError.unreadable }
struct Evidence: Encodable {
    let passages: [Passage]
    let pageText: String
    let size: [Double]
    let image: Data
}
let output = Evidence(passages: passages, pageText: text, size: [box.width, box.height], image: png)
FileHandle.standardOutput.write(try JSONEncoder().encode(output))
