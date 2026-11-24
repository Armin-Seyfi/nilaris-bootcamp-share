// Read only the vertical positions of the two known labels in a Chrome screenshot.
import Foundation
import Vision

let screenshot = FileHandle.standardInput.readDataToEndOfFile()
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
try VNImageRequestHandler(data: screenshot).perform([request])
var positions: [String: Double] = [:]
for observation in request.results ?? [] {
    let text = observation.topCandidates(1).first?.string.lowercased().filter { $0.isLetter } ?? ""
    for label in ["flowrated", "flownormal"] where text.contains(label) {
        positions[label] = 1 - observation.boundingBox.midY
    }
}
FileHandle.standardOutput.write(try JSONEncoder().encode(positions))
