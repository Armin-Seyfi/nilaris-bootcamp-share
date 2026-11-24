// Prepare a private, local Reviewer bundle for the selected, pinned page layout.
// Usage: swift prepare.swift TABLE_PDF [NOTE_PDF] NEW_PRIVATE_DIRECTORY
import AppKit
import CryptoKit
import Foundation
import PDFKit

enum PreparationError: Error { case wrongRevision, unexpectedPage, unexpectedLayout, writeFailure }
func require(_ condition: Bool, _ error: PreparationError) throws {
    if !condition { throw error }
}
func digest(_ bytes: Data) -> String {
    SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
}
let section = "PUMP TECHNICAL DATA"

struct ObservedRow: Encodable {
    let number: Int
    let label: String
    let value: String
    let unit: String
    let text: String
    let bounds: [CGFloat]
    let multiline: Bool
    let pageMidY: CGFloat

    enum CodingKeys: String, CodingKey {
        case number, label, value, unit, text, bounds, multiline
    }
}

func observedRows(_ page: PDFPage, in doc: PDFDocument) throws -> [ObservedRow] {
    func heading(_ name: String) throws -> CGRect {
        let hits = doc.findString(name, withOptions: []).filter { $0.pages.first == page }
        try require(hits.count == 1, .unexpectedLayout)
        return hits[0].bounds(for: page)
    }
    let upper = try heading(section).minY
    let lower = try heading("CONDITIONS OF USE").maxY
    let box = page.bounds(for: .cropBox)
    try require(upper > lower && page.rotation == 0 && box.origin == .zero, .unexpectedPage)
    // The geometry describes the columns on this pinned layout. The printed
    // row numbers, including otherwise empty rows, determine what gets read.
    let column = CGRect(x: 55, y: lower, width: 16, height: upper - lower)
    let anchors = (page.selection(for: column)?.selectionsByLine() ?? [])
        .sorted { $0.bounds(for: page).midY > $1.bounds(for: page).midY }
    let centers = anchors.map { $0.bounds(for: page).midY }
    let pitches = zip(centers, centers.dropFirst()).map { higher, lower in higher - lower }.sorted()
    try require(!pitches.isEmpty, .unexpectedLayout)
    let pitch = pitches[pitches.count / 2]
    // A tall, nested row may share a number across several printed lines.
    // The previous single line ends half a normal spacing below its number.
    let dividers = zip(centers, centers.dropFirst()).map { higher, lower in
        max(higher - pitch / 2, (higher + lower) / 2)
    }
    let edges = [upper] + dividers + [lower]
    func read(_ rect: CGRect) -> String {
        return page.selection(for: rect)?.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }
    return try anchors.enumerated().map { index, mark in
        let rawNumber = mark.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let number = Int(rawNumber), number == index + 1 else {
            throw PreparationError.unexpectedLayout
        }
        let center = centers[index]
        let top = edges[index]
        let bottom = edges[index + 1]
        let rect = CGRect(x: 55, y: bottom, width: 261, height: top - bottom)
        let multiline = rect.height > 1.5 * pitch
        func cell(_ left: CGFloat, _ right: CGFloat) -> String {
            read(CGRect(x: left, y: center - 3.8, width: right - left, height: 7.6))
        }
        return ObservedRow(number: number, label: cell(72, 201), value: cell(202, 273),
                           unit: cell(274, 316), text: read(rect),
                           bounds: [rect.minX / box.width, (box.maxY - top) / box.height,
                                    rect.width / box.width, rect.height / box.height],
                           multiline: multiline, pageMidY: rect.midY)
    }
}

func render(_ page: PDFPage) throws -> Data {
    let box = page.bounds(for: .cropBox)
    let size = NSSize(width: box.width * 3, height: box.height * 3)
    let image = page.thumbnail(of: size, for: .cropBox)
    guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
          let png = bitmap.representation(using: .png, properties: [:]) else {
        throw PreparationError.unexpectedPage
    }
    return png
}

// On this revision the seventh and sixth remarks share one printed line.
// Use the ordinal and the line's actual character range to retain the entire note.
func noteEvidence(_ bytes: Data) throws -> Data {
    guard let doc = PDFDocument(data: bytes), let page = doc.page(at: 0),
          let text = page.string else { throw PreparationError.unexpectedPage }
    let box = page.bounds(for: .cropBox)
    try require(page.rotation == 0 && box.origin == .zero, .unexpectedPage)
    func unique(_ phrase: String) throws -> PDFSelection {
        let hits = doc.findString(phrase, withOptions: []).filter { $0.pages.first == page }
        try require(hits.count == 1, .unexpectedLayout)
        return hits[0]
    }
    let phrase = try unique("pump shut off pressure")
    let reference = try unique("Pump design pressure(7)")
    let range = phrase.range(at: 0, on: page)
    let source = text as NSString
    let line = source.lineRange(for: range)
    let marker = source.range(of: "7)", options: .backwards,
                              range: NSRange(location: line.location, length: range.location - line.location))
    try require(marker.location != NSNotFound, .unexpectedLayout)
    let full = NSRange(location: marker.location, length: NSMaxRange(line) - marker.location)
    let passage = source.substring(with: full).trimmingCharacters(in: .whitespacesAndNewlines)
    guard let selected = page.selection(for: NSRange(location: marker.location,
                                                      length: (passage as NSString).length)) else {
        throw PreparationError.unexpectedLayout
    }
    let rect = selected.bounds(for: page)
    let evidence: [String: Any] = [
        "passage": passage, "expression": phrase.string!, "context": reference.string!,
        "page_size": [box.width, box.height],
        "bounds": [rect.minX / box.width, (box.maxY - rect.maxY) / box.height,
                   rect.width / box.width, rect.height / box.height],
        "image": try render(page).base64EncodedString()
    ]
    return try JSONSerialization.data(withJSONObject: evidence)
}

func revisionPin(_ filename: String) throws -> String {
    let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    let file = repo.appendingPathComponent("src/nilaris/review_web").appendingPathComponent(filename)
    return try String(contentsOf: file, encoding: .utf8).trimmingCharacters(in: .newlines)
}

func prepare(source: URL, note: URL?, destination: URL) throws {
    let pdf = try Data(contentsOf: source)
    let sha = digest(pdf)
    try require(sha == revisionPin("rfq_revision.txt"), .wrongRevision)
    let noteBytes = try note.map { try Data(contentsOf: $0) }
    if let bytes = noteBytes {
        try require(digest(bytes) == revisionPin("rfq_note_revision.txt"), .wrongRevision)
    }
    guard let doc = PDFDocument(data: pdf), let page = doc.page(at: 0),
          let pageText = page.string else { throw PreparationError.unexpectedPage }
    let box = page.bounds(for: .cropBox)
    try require(page.rotation == 0 && box.minX == 0 && box.minY == 0, .unexpectedPage)
    let rows = try observedRows(page, in: doc)
    let first = rows[0], second = rows[1]
    // Retain the original fixture workspace alongside the byte-derived draft.
    func passage(_ row: ObservedRow) -> String {
        [String(row.number), row.label, row.value, row.unit].joined(separator: "\t")
    }
    let unrelated = page.selection(for: CGRect(x: 331, y: first.pageMidY - 4, width: 120, height: 8))?
        .string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let png = try render(page)
    var manifest: [String: Any] = [
        "sha256": sha, "filename": source.lastPathComponent, "page": 1,
        "section": section, "row": 1, "attribute": first.label, "unit": first.unit,
        "comparison_attribute": second.label, "passage": passage(first),
        "qualifier_comparison": passage(second), "unrelated_passage": unrelated,
        "page_text": pageText, "bounds": first.bounds, "comparison_bounds": second.bounds, "original": "original.pdf",
        "page_size": [box.width, box.height], "image": "page.png", "image_sha256": digest(png)
    ]
    if noteBytes != nil { manifest["note_original"] = "note-original.pdf" }
    let json = try JSONSerialization.data(withJSONObject: manifest, options: [.sortedKeys])
    let fm = FileManager.default
    // POSIX mkdir refuses a preexisting destination, including a symlink.
    try require(mkdir(destination.path, 0o700) == 0, .writeFailure)
    do {
        try noteBytes?.write(to: destination.appendingPathComponent("note-original.pdf"), options: .withoutOverwriting)
        for (name, data) in [("original.pdf", pdf), ("page.png", png), ("manifest.json", json)] {
            try data.write(to: destination.appendingPathComponent(name), options: .withoutOverwriting)
        }
    } catch {
        try? fm.removeItem(at: destination)
        throw error
    }
}
// Re-read the verified PDF snapshot on Reviewer startup. The manifest's
// historical fixture observations never supply Schema draft rows.
if CommandLine.arguments.count == 2 && ["--schema", "--note"].contains(CommandLine.arguments[1]) {
    do {
        let bytes = FileHandle.standardInput.readDataToEndOfFile()
        if CommandLine.arguments[1] == "--note" {
            FileHandle.standardOutput.write(try noteEvidence(bytes))
            exit(0)
        }
        guard let doc = PDFDocument(data: bytes), let page = doc.page(at: 0) else {
            throw PreparationError.unexpectedPage
        }
        FileHandle.standardOutput.write(try JSONEncoder().encode(observedRows(page, in: doc)))
        exit(0)
    } catch {
        fputs("Could not read the pinned pump table.\n", stderr)
        exit(1)
    }
}
guard [3, 4].contains(CommandLine.arguments.count) else {
    fputs("Usage: swift prepare.swift TABLE_PDF [NOTE_PDF] NEW_PRIVATE_DIRECTORY\n", stderr)
    exit(2)
}
umask(0o077)
do {
    try prepare(source: URL(fileURLWithPath: CommandLine.arguments[1]),
                note: CommandLine.arguments.count == 4 ? URL(fileURLWithPath: CommandLine.arguments[2]) : nil,
                destination: URL(fileURLWithPath: CommandLine.arguments.last!))
    print("Verified the PDF revision and the numbered pump table. Private bundle ready.")
} catch {
    fputs("Could not verify or prepare the selected local PDF: \(error)\n", stderr)
    exit(1)
}
