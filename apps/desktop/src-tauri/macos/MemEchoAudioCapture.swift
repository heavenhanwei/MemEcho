import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// Binary protocol: stdout contains only 16 kHz mono PCM16 little-endian.
// Diagnostics go to stderr. Exit 20 means microphone permission is missing;
// exit 21 means Screen Recording/System Audio permission is missing.

private let targetRate = 16_000.0
private let samplesPerTick = 320 // 20 ms at 16 kHz

private enum Source: String {
    case mic
    case system
    case mixed
}

private func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

private final class PCMQueue {
    private var samples: [Int16] = []
    private let lock = NSLock()

    func append(_ values: [Int16]) {
        guard !values.isEmpty else { return }
        lock.lock()
        samples.append(contentsOf: values)
        lock.unlock()
    }

    func take(_ count: Int) -> [Int16] {
        lock.lock()
        defer { lock.unlock() }
        let available = min(count, samples.count)
        let result = Array(samples.prefix(available))
        if available > 0 { samples.removeFirst(available) }
        return result
    }
}

private final class PCMEncoder {
    private var previous: Float = 0
    private var phase: Double = 1

    func convert(_ buffer: AVAudioPCMBuffer) -> [Int16] {
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        guard frames > 0, channels > 0 else { return [] }

        var mono = [Float](repeating: 0, count: frames)
        switch buffer.format.commonFormat {
        case .pcmFormatFloat32:
            guard let data = buffer.floatChannelData else { return [] }
            for channel in 0..<channels {
                for frame in 0..<frames { mono[frame] += data[channel][frame] }
            }
        case .pcmFormatInt16:
            guard let data = buffer.int16ChannelData else { return [] }
            for channel in 0..<channels {
                for frame in 0..<frames {
                    mono[frame] += Float(data[channel][frame]) / 32_768.0
                }
            }
        case .pcmFormatInt32:
            guard let data = buffer.int32ChannelData else { return [] }
            for channel in 0..<channels {
                for frame in 0..<frames {
                    mono[frame] += Float(data[channel][frame]) / 2_147_483_648.0
                }
            }
        default:
            return []
        }
        let divisor = Float(channels)
        for index in mono.indices { mono[index] /= divisor }

        let sourceRate = buffer.format.sampleRate
        guard sourceRate > 0 else { return [] }
        let ratio = sourceRate / targetRate
        var output: [Int16] = []
        for sample in mono {
            while phase < 1.0 {
                let fraction = Float(phase)
                let interpolated = previous * (1 - fraction) + sample * fraction
                output.append(Int16(max(-1, min(1, interpolated)) * 32_767))
                phase += ratio
            }
            phase -= 1.0
            previous = sample
        }
        return output
    }
}

private final class AudioCoordinator: NSObject, SCStreamOutput, SCStreamDelegate {
    private let source: Source
    private let micQueue = PCMQueue()
    private let systemQueue = PCMQueue()
    private let micEncoder = PCMEncoder()
    private let systemEncoder = PCMEncoder()
    private let writeLock = NSLock()
    private var audioEngine: AVAudioEngine?
    private var stream: SCStream?
    private var timer: DispatchSourceTimer?

    init(source: Source) {
        self.source = source
    }

    func start() async throws {
        if source == .mic || source == .mixed {
            let permission = AVCaptureDevice.authorizationStatus(for: .audio)
            let granted: Bool
            if permission == .notDetermined {
                granted = await AVCaptureDevice.requestAccess(for: .audio)
            } else {
                granted = permission == .authorized
            }
            guard granted else { throw CaptureFailure.microphonePermission }
            try startMicrophone()
        }
        if source == .system || source == .mixed { try await startSystemAudio() }
        // A fixed 20 ms clock preserves the WAV/ASR timeline during silence
        // and guarantees the parent can interrupt a blocking stdout reader.
        startMixer()
    }

    private func emit(_ samples: [Int16]) {
        guard !samples.isEmpty else { return }
        var mutable = samples
        let data = mutable.withUnsafeMutableBytes { Data($0) }
        writeLock.lock()
        FileHandle.standardOutput.write(data)
        writeLock.unlock()
    }

    private func startMicrophone() throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CaptureFailure.noMicrophone
        }
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let pcm = self.micEncoder.convert(buffer)
            self.micQueue.append(pcm)
        }
        engine.prepare()
        try engine.start()
        audioEngine = engine
    }

    private func startSystemAudio() async throws {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            throw CaptureFailure.screenPermission
        }
        guard let display = content.displays.first else { throw CaptureFailure.noDisplay }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = Int(targetRate)
        configuration.channelCount = 1
        // A tiny/slow video stream minimizes GPU work; only audio is consumed.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3

        let capture = SCStream(filter: filter, configuration: configuration, delegate: self)
        try capture.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "com.memecho.system-audio"))
        try await capture.startCapture()
        stream = capture
    }

    private func startMixer() {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "com.memecho.audio-mixer"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(20))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let mic = self.micQueue.take(samplesPerTick)
            let system = self.systemQueue.take(samplesPerTick)
            var mixed = [Int16](repeating: 0, count: samplesPerTick)
            for index in 0..<samplesPerTick {
                let left = index < mic.count ? Int32(mic[index]) : 0
                let right = index < system.count ? Int32(system[index]) : 0
                if self.source == .mic {
                    mixed[index] = Int16(left)
                } else if self.source == .system {
                    mixed[index] = Int16(right)
                } else if index < mic.count && index < system.count {
                    mixed[index] = Int16(max(-32_768, min(32_767, (left + right) / 2)))
                } else {
                    mixed[index] = Int16(max(-32_768, min(32_767, left + right)))
                }
            }
            self.emit(mixed)
        }
        timer.resume()
        self.timer = timer
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        // Follow Apple's ScreenCaptureKit sample: borrow the AudioBufferList
        // for the callback lifetime and wrap it without copying.
        try? sampleBuffer.withAudioBufferList { audioBufferList, _ in
            guard let description = sampleBuffer.formatDescription?.audioStreamBasicDescription,
                  let format = AVAudioFormat(
                    standardFormatWithSampleRate: description.mSampleRate,
                    channels: description.mChannelsPerFrame
                  ),
                  let pcmBuffer = AVAudioPCMBuffer(
                    pcmFormat: format,
                    bufferListNoCopy: audioBufferList.unsafePointer
                  )
            else { return }
            let pcm = systemEncoder.convert(pcmBuffer)
            systemQueue.append(pcm)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(Data(("ScreenCaptureKit stopped: \(error)\n").utf8))
        exit(21)
    }
}

private enum CaptureFailure: Error {
    case microphonePermission
    case screenPermission
    case noMicrophone
    case noDisplay
}

private func parseSource() -> Source? {
    guard let index = CommandLine.arguments.firstIndex(of: "--source"),
          CommandLine.arguments.indices.contains(index + 1)
    else { return nil }
    return Source(rawValue: CommandLine.arguments[index + 1])
}

guard #available(macOS 13.0, *) else {
    fail("memEcho requires macOS 13 or later", code: 22)
}
guard let source = parseSource() else {
    fail("usage: memecho-audio-capture --source mic|system|mixed", code: 2)
}

private let coordinator = AudioCoordinator(source: source)
Task {
    do {
        try await coordinator.start()
    } catch CaptureFailure.microphonePermission {
        fail("microphone permission is required", code: 20)
    } catch CaptureFailure.screenPermission {
        fail("screen and system audio recording permission is required", code: 21)
    } catch {
        fail("audio capture startup failed: \(error)", code: 23)
    }
}
RunLoop.main.run()
