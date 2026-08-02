use serde::Serialize;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

const SAMPLE_RATE: u32 = 16_000;
const CHANNELS: u16 = 1;
const BITS_PER_SAMPLE: u16 = 16;
const BLOCK_ALIGN: u16 = CHANNELS * BITS_PER_SAMPLE / 8;
const BYTE_RATE: u32 = SAMPLE_RATE * BLOCK_ALIGN as u32;
const MAX_CLIP_MS: u64 = 60_000;

#[derive(Debug, Clone, Serialize)]
pub struct EvidenceClip {
    pub mime_type: &'static str,
    pub data_base64: String,
    pub duration_ms: u64,
    pub start_ms: u64,
    pub end_ms: u64,
    pub track: String,
}

#[derive(Debug, thiserror::Error)]
pub enum EvidenceClipError {
    #[error("invalid session id")]
    InvalidSession,
    #[error("track must be mic or system")]
    InvalidTrack,
    #[error("clip range must have endMs greater than startMs")]
    InvalidRange,
    #[error("evidence clips are limited to 60 seconds")]
    ClipTooLong,
    #[error("local session is unavailable")]
    SessionUnavailable,
    #[error("requested WAV track is unavailable")]
    TrackUnavailable,
    #[error("imported MP3, M4A, and MP4 evidence playback is not supported")]
    UnsupportedImportedMedia,
    #[error("evidence track is outside the private session directory")]
    PathTraversal,
    #[error("evidence WAV could not be read")]
    ReadFailed,
    #[error("evidence file is not a valid WAV")]
    InvalidWav,
    #[error("evidence WAV must be 16 kHz mono 16-bit PCM")]
    UnsupportedWavFormat,
    #[error("requested range is outside the available audio")]
    RangeOutsideAudio,
}

#[derive(Debug)]
struct WavLayout {
    data_offset: u64,
    data_bytes: u64,
}

fn read_u16(bytes: &[u8]) -> u16 {
    u16::from_le_bytes([bytes[0], bytes[1]])
}

fn read_u32(bytes: &[u8]) -> u32 {
    u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

fn inspect_wav(file: &mut File) -> Result<WavLayout, EvidenceClipError> {
    let file_len = file
        .metadata()
        .map_err(|_| EvidenceClipError::ReadFailed)?
        .len();
    if file_len < 44 {
        return Err(EvidenceClipError::InvalidWav);
    }

    let mut riff = [0u8; 12];
    file.read_exact(&mut riff)
        .map_err(|_| EvidenceClipError::ReadFailed)?;
    if &riff[0..4] != b"RIFF" || &riff[8..12] != b"WAVE" {
        return Err(EvidenceClipError::InvalidWav);
    }
    let riff_end = u64::from(read_u32(&riff[4..8]))
        .checked_add(8)
        .ok_or(EvidenceClipError::InvalidWav)?;
    if riff_end > file_len || riff_end < 44 {
        return Err(EvidenceClipError::InvalidWav);
    }

    let mut format: Option<(u16, u16, u32, u32, u16, u16)> = None;
    let mut data: Option<(u64, u64)> = None;
    let mut cursor = 12u64;

    while cursor.checked_add(8).is_some_and(|end| end <= riff_end) {
        file.seek(SeekFrom::Start(cursor))
            .map_err(|_| EvidenceClipError::ReadFailed)?;
        let mut chunk_header = [0u8; 8];
        file.read_exact(&mut chunk_header)
            .map_err(|_| EvidenceClipError::ReadFailed)?;
        let chunk_size = u64::from(read_u32(&chunk_header[4..8]));
        let chunk_start = cursor.checked_add(8).ok_or(EvidenceClipError::InvalidWav)?;
        let chunk_end = chunk_start
            .checked_add(chunk_size)
            .ok_or(EvidenceClipError::InvalidWav)?;
        if chunk_end > riff_end {
            return Err(EvidenceClipError::InvalidWav);
        }

        if &chunk_header[0..4] == b"fmt " {
            if chunk_size < 16 {
                return Err(EvidenceClipError::InvalidWav);
            }
            let mut fmt = [0u8; 16];
            file.read_exact(&mut fmt)
                .map_err(|_| EvidenceClipError::ReadFailed)?;
            format = Some((
                read_u16(&fmt[0..2]),
                read_u16(&fmt[2..4]),
                read_u32(&fmt[4..8]),
                read_u32(&fmt[8..12]),
                read_u16(&fmt[12..14]),
                read_u16(&fmt[14..16]),
            ));
        } else if &chunk_header[0..4] == b"data" {
            data = Some((chunk_start, chunk_size));
        }

        cursor = chunk_end
            .checked_add(chunk_size % 2)
            .ok_or(EvidenceClipError::InvalidWav)?;
    }

    let (audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample) =
        format.ok_or(EvidenceClipError::InvalidWav)?;
    if audio_format != 1
        || channels != CHANNELS
        || sample_rate != SAMPLE_RATE
        || byte_rate != BYTE_RATE
        || block_align != BLOCK_ALIGN
        || bits_per_sample != BITS_PER_SAMPLE
    {
        return Err(EvidenceClipError::UnsupportedWavFormat);
    }

    let (data_offset, data_bytes) = data.ok_or(EvidenceClipError::InvalidWav)?;
    if data_bytes % u64::from(BLOCK_ALIGN) != 0 {
        return Err(EvidenceClipError::InvalidWav);
    }
    Ok(WavLayout {
        data_offset,
        data_bytes,
    })
}

fn wav_header(data_bytes: u32) -> [u8; 44] {
    let mut header = [0u8; 44];
    header[0..4].copy_from_slice(b"RIFF");
    header[4..8].copy_from_slice(&(36 + data_bytes).to_le_bytes());
    header[8..12].copy_from_slice(b"WAVE");
    header[12..16].copy_from_slice(b"fmt ");
    header[16..20].copy_from_slice(&16u32.to_le_bytes());
    header[20..22].copy_from_slice(&1u16.to_le_bytes());
    header[22..24].copy_from_slice(&CHANNELS.to_le_bytes());
    header[24..28].copy_from_slice(&SAMPLE_RATE.to_le_bytes());
    header[28..32].copy_from_slice(&BYTE_RATE.to_le_bytes());
    header[32..34].copy_from_slice(&BLOCK_ALIGN.to_le_bytes());
    header[34..36].copy_from_slice(&BITS_PER_SAMPLE.to_le_bytes());
    header[36..40].copy_from_slice(b"data");
    header[40..44].copy_from_slice(&data_bytes.to_le_bytes());
    header
}

fn encode_base64(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = Vec::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let first = chunk[0];
        let second = chunk.get(1).copied().unwrap_or(0);
        let third = chunk.get(2).copied().unwrap_or(0);
        output.push(TABLE[(first >> 2) as usize]);
        output.push(TABLE[(((first & 0x03) << 4) | (second >> 4)) as usize]);
        output.push(if chunk.len() > 1 {
            TABLE[(((second & 0x0f) << 2) | (third >> 6)) as usize]
        } else {
            b'='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(third & 0x3f) as usize]
        } else {
            b'='
        });
    }
    String::from_utf8(output).expect("base64 alphabet is valid UTF-8")
}

fn contains_unsupported_import(session_dir: &Path) -> bool {
    std::fs::read_dir(session_dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| entry.path().extension().map(|value| value.to_owned()))
        .filter_map(|extension| extension.to_str().map(str::to_ascii_lowercase))
        .any(|extension| matches!(extension.as_str(), "mp3" | "m4a" | "mp4"))
}

pub fn read_evidence_clip_impl(
    session_id: &str,
    track: &str,
    start_ms: u64,
    end_ms: u64,
    sessions_dir: &Path,
) -> Result<EvidenceClip, EvidenceClipError> {
    if start_ms >= end_ms {
        return Err(EvidenceClipError::InvalidRange);
    }
    if end_ms - start_ms > MAX_CLIP_MS {
        return Err(EvidenceClipError::ClipTooLong);
    }

    let session_dir = crate::paths::validate_session_path(session_id, sessions_dir)
        .map_err(|_| EvidenceClipError::InvalidSession)?;
    if !session_dir.is_dir() {
        return Err(EvidenceClipError::SessionUnavailable);
    }
    let canonical_session = session_dir
        .canonicalize()
        .map_err(|_| EvidenceClipError::SessionUnavailable)?;

    let file_name = match track {
        "mic" => "mic.wav",
        "system" => "loopback.wav",
        _ => return Err(EvidenceClipError::InvalidTrack),
    };
    let track_path = session_dir.join(file_name);
    if !track_path.is_file() {
        return Err(if contains_unsupported_import(&session_dir) {
            EvidenceClipError::UnsupportedImportedMedia
        } else {
            EvidenceClipError::TrackUnavailable
        });
    }
    let canonical_track = track_path
        .canonicalize()
        .map_err(|_| EvidenceClipError::TrackUnavailable)?;
    if canonical_track.parent() != Some(canonical_session.as_path()) {
        return Err(EvidenceClipError::PathTraversal);
    }

    let mut file = File::open(&canonical_track).map_err(|_| EvidenceClipError::ReadFailed)?;
    let layout = inspect_wav(&mut file)?;
    let total_frames = layout.data_bytes / u64::from(BLOCK_ALIGN);
    let start_frame = start_ms
        .checked_mul(u64::from(SAMPLE_RATE))
        .ok_or(EvidenceClipError::RangeOutsideAudio)?
        / 1000;
    let end_frame = end_ms
        .checked_mul(u64::from(SAMPLE_RATE))
        .ok_or(EvidenceClipError::RangeOutsideAudio)?
        / 1000;
    if end_frame > total_frames {
        return Err(EvidenceClipError::RangeOutsideAudio);
    }

    let start_byte = start_frame
        .checked_mul(u64::from(BLOCK_ALIGN))
        .ok_or(EvidenceClipError::RangeOutsideAudio)?;
    let clip_bytes = end_frame
        .checked_sub(start_frame)
        .and_then(|frames| frames.checked_mul(u64::from(BLOCK_ALIGN)))
        .ok_or(EvidenceClipError::RangeOutsideAudio)?;
    let clip_len = usize::try_from(clip_bytes).map_err(|_| EvidenceClipError::RangeOutsideAudio)?;
    let data_len = u32::try_from(clip_bytes).map_err(|_| EvidenceClipError::RangeOutsideAudio)?;

    file.seek(SeekFrom::Start(
        layout
            .data_offset
            .checked_add(start_byte)
            .ok_or(EvidenceClipError::RangeOutsideAudio)?,
    ))
    .map_err(|_| EvidenceClipError::ReadFailed)?;
    let mut output = Vec::with_capacity(44 + clip_len);
    output.extend_from_slice(&wav_header(data_len));
    output.resize(44 + clip_len, 0);
    file.read_exact(&mut output[44..])
        .map_err(|_| EvidenceClipError::ReadFailed)?;

    Ok(EvidenceClip {
        mime_type: "audio/wav",
        data_base64: encode_base64(&output),
        duration_ms: (end_frame - start_frame) * 1000 / u64::from(SAMPLE_RATE),
        start_ms,
        end_ms,
        track: track.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::wav::create_streaming_wav;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ID: AtomicU64 = AtomicU64::new(1);

    fn fixture(sample_count: usize) -> (std::path::PathBuf, String) {
        let root = std::env::temp_dir().join(format!(
            "memecho_evidence_{}_{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&root).unwrap();
        let session_id = "session-123".to_string();
        let session_dir = root.join(&session_id);
        std::fs::create_dir_all(&session_dir).unwrap();
        let mut wav = create_streaming_wav(&session_dir.join("mic.wav"), SAMPLE_RATE).unwrap();
        let pcm = vec![7i16; sample_count]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect::<Vec<_>>();
        wav.append(&pcm).unwrap();
        wav.finalize().unwrap();
        (root, session_id)
    }

    fn decode_base64(input: &str) -> Vec<u8> {
        let mut output = Vec::new();
        let mut block = [0u8; 4];
        let mut count = 0;
        for byte in input.bytes().filter(|byte| !byte.is_ascii_whitespace()) {
            block[count] = match byte {
                b'A'..=b'Z' => byte - b'A',
                b'a'..=b'z' => byte - b'a' + 26,
                b'0'..=b'9' => byte - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                b'=' => 64,
                _ => panic!("invalid base64"),
            };
            count += 1;
            if count == 4 {
                output.push((block[0] << 2) | (block[1] >> 4));
                if block[2] != 64 {
                    output.push((block[1] << 4) | (block[2] >> 2));
                }
                if block[3] != 64 {
                    output.push((block[2] << 6) | block[3]);
                }
                count = 0;
            }
        }
        assert_eq!(count, 0);
        output
    }

    #[test]
    fn extracts_a_valid_one_second_wav_without_returning_a_path() {
        let (root, session_id) = fixture(SAMPLE_RATE as usize * 3);
        let clip = read_evidence_clip_impl(&session_id, "mic", 500, 1500, &root).unwrap();
        let wav = decode_base64(&clip.data_base64);

        assert_eq!(clip.duration_ms, 1000);
        assert_eq!(clip.mime_type, "audio/wav");
        assert_eq!(wav.len(), 44 + BYTE_RATE as usize);
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(read_u32(&wav[40..44]), BYTE_RATE);
        let serialized = serde_json::to_string(&clip).unwrap();
        assert!(!serialized.contains(root.to_string_lossy().as_ref()));

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn enforces_range_boundaries_and_the_sixty_second_limit() {
        let (root, session_id) = fixture(SAMPLE_RATE as usize * 61);

        assert!(matches!(
            read_evidence_clip_impl(&session_id, "mic", 1000, 1000, &root),
            Err(EvidenceClipError::InvalidRange)
        ));
        assert!(matches!(
            read_evidence_clip_impl(&session_id, "mic", 0, 60_001, &root),
            Err(EvidenceClipError::ClipTooLong)
        ));
        let maximum = read_evidence_clip_impl(&session_id, "mic", 0, 60_000, &root).unwrap();
        assert_eq!(maximum.duration_ms, 60_000);

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_ranges_beyond_available_audio() {
        let (root, session_id) = fixture(SAMPLE_RATE as usize);

        assert!(matches!(
            read_evidence_clip_impl(&session_id, "mic", 500, 1500, &root),
            Err(EvidenceClipError::RangeOutsideAudio)
        ));

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_traversal_and_unknown_tracks() {
        let (root, session_id) = fixture(SAMPLE_RATE as usize);

        assert!(matches!(
            read_evidence_clip_impl("../session-123", "mic", 0, 100, &root),
            Err(EvidenceClipError::InvalidSession)
        ));
        assert!(matches!(
            read_evidence_clip_impl(&session_id, "../mic.wav", 0, 100, &root),
            Err(EvidenceClipError::InvalidTrack)
        ));

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_wrong_wav_format_and_unsupported_imported_media() {
        let (root, session_id) = fixture(SAMPLE_RATE as usize);
        let session_dir = root.join(&session_id);
        std::fs::write(session_dir.join("mic.wav"), b"not a wav").unwrap();
        assert!(matches!(
            read_evidence_clip_impl(&session_id, "mic", 0, 100, &root),
            Err(EvidenceClipError::InvalidWav)
        ));

        std::fs::remove_file(session_dir.join("mic.wav")).unwrap();
        std::fs::write(session_dir.join("source.mp3"), b"ID3").unwrap();
        assert!(matches!(
            read_evidence_clip_impl(&session_id, "mic", 0, 100, &root),
            Err(EvidenceClipError::UnsupportedImportedMedia)
        ));

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn base64_encoder_covers_padding_boundaries() {
        assert_eq!(encode_base64(b""), "");
        assert_eq!(encode_base64(b"f"), "Zg==");
        assert_eq!(encode_base64(b"fo"), "Zm8=");
        assert_eq!(encode_base64(b"foo"), "Zm9v");
    }
}
