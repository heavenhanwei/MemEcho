use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};

#[derive(Debug, thiserror::Error)]
pub enum WavError {
    #[error("io error: {0}")]
    Io(std::io::Error),
}

/// Create a new streaming WAV file with a placeholder 44-byte header.
/// The returned `StreamingWav` owns a `BufWriter<File>` that stays open
/// for the lifetime of the capture — no repeated open/close per packet.
pub fn create_streaming_wav(
    path: &std::path::Path,
    sample_rate: u32,
) -> Result<StreamingWav, WavError> {
    let file = File::create(path).map_err(WavError::Io)?;
    let mut w = BufWriter::new(file);

    let nch: u16 = 1;
    let bps: u16 = 16;
    let br = sample_rate * nch as u32 * bps as u32 / 8;
    let ba = nch * bps / 8;

    w.write_all(b"RIFF").map_err(WavError::Io)?;
    w.write_all(&[0u8; 4]).map_err(WavError::Io)?;
    w.write_all(b"WAVE").map_err(WavError::Io)?;
    w.write_all(b"fmt ").map_err(WavError::Io)?;
    w.write_all(&16u32.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(&1u16.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(&nch.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(&sample_rate.to_le_bytes())
        .map_err(WavError::Io)?;
    w.write_all(&br.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(&ba.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(&bps.to_le_bytes()).map_err(WavError::Io)?;
    w.write_all(b"data").map_err(WavError::Io)?;
    w.write_all(&[0u8; 4]).map_err(WavError::Io)?;
    w.flush().map_err(WavError::Io)?;

    Ok(StreamingWav {
        w,
        data_bytes: 0,
        flushed_bytes: 0,
    })
}

/// Streaming WAV writer. Holds a `BufWriter<File>` open for the entire
/// capture lifetime — `append` never re-opens the file.
pub struct StreamingWav {
    w: BufWriter<File>,
    data_bytes: u64,
    /// Bytes confirmed on disk via `flush_safe`.
    flushed_bytes: u64,
}

impl StreamingWav {
    /// Append raw i16 PCM bytes to the data chunk.
    pub fn append(&mut self, pcm: &[u8]) -> Result<(), WavError> {
        self.w.write_all(pcm).map_err(WavError::Io)?;
        self.data_bytes += pcm.len() as u64;
        Ok(())
    }

    /// Flush BufWriter + fsync data to disk. Returns the number of
    /// bytes confirmed durable on success. The caller should only
    /// update the shared `AtomicU64` with this returned value.
    pub fn flush_safe(&mut self) -> Result<u64, WavError> {
        self.w.flush().map_err(WavError::Io)?;
        self.w.get_ref().sync_data().map_err(WavError::Io)?;
        self.flushed_bytes = self.data_bytes;
        Ok(self.flushed_bytes)
    }

    /// Finalize the WAV header with actual sizes. Rewrites only the
    /// 8 bytes of RIFF size + data size at offsets 4 and 40.
    pub fn finalize(&mut self) -> Result<(), WavError> {
        self.w.flush().map_err(WavError::Io)?;
        let ds = self.data_bytes as u32;
        let rs = 36 + ds;
        self.w.seek(SeekFrom::Start(4)).map_err(WavError::Io)?;
        self.w.write_all(&rs.to_le_bytes()).map_err(WavError::Io)?;
        self.w.seek(SeekFrom::Start(40)).map_err(WavError::Io)?;
        self.w.write_all(&ds.to_le_bytes()).map_err(WavError::Io)?;
        self.w.flush().map_err(WavError::Io)?;
        self.w.get_ref().sync_data().map_err(WavError::Io)?;
        Ok(())
    }

    pub fn data_bytes(&self) -> u64 {
        self.data_bytes
    }

    pub fn flushed_bytes(&self) -> u64 {
        self.flushed_bytes
    }
}

/// Truncate a WAV file to `safe_data_bytes` of PCM and rewrite
/// RIFF/data-size headers in-place. Does NOT read the entire file.
pub fn truncate_and_fixup_wav(
    wav_path: &std::path::Path,
    safe_data_bytes: u64,
) -> Result<(), WavError> {
    let mut f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(wav_path)
        .map_err(WavError::Io)?;

    let file_len = f.seek(SeekFrom::End(0)).map_err(WavError::Io)?;
    let target = 44 + safe_data_bytes;
    if target < file_len {
        f.set_len(target).map_err(WavError::Io)?;
    }

    let ds = safe_data_bytes.min(file_len.saturating_sub(44)) as u32;
    let rs = 36 + ds;

    f.seek(SeekFrom::Start(4)).map_err(WavError::Io)?;
    f.write_all(&rs.to_le_bytes()).map_err(WavError::Io)?;
    f.seek(SeekFrom::Start(40)).map_err(WavError::Io)?;
    f.write_all(&ds.to_le_bytes()).map_err(WavError::Io)?;
    f.flush().map_err(WavError::Io)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_streaming_wav_roundtrip() {
        let dir = std::env::temp_dir().join("memecho_t_sw_rt");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.wav");

        let mut w = create_streaming_wav(&p, 16000).unwrap();
        assert_eq!(w.data_bytes(), 0);

        let c1: Vec<u8> = vec![0i16; 100]
            .iter()
            .flat_map(|s| s.to_le_bytes())
            .collect();
        w.append(&c1).unwrap();
        let c2: Vec<u8> = vec![100i16; 50]
            .iter()
            .flat_map(|s| s.to_le_bytes())
            .collect();
        w.append(&c2).unwrap();

        assert_eq!(w.data_bytes(), 300);
        w.finalize().unwrap();

        let d = std::fs::read(&p).unwrap();
        assert_eq!(&d[0..4], b"RIFF");
        assert_eq!(&d[8..12], b"WAVE");
        assert_eq!(u32::from_le_bytes([d[40], d[41], d[42], d[43]]), 300);
        assert_eq!(u32::from_le_bytes([d[4], d[5], d[6], d[7]]), 36 + 300);
        assert_eq!(&d[44..46], &[0, 0]);
        assert_eq!(&d[244..246], &100i16.to_le_bytes());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_streaming_wav_sample_rate() {
        let dir = std::env::temp_dir().join("memecho_t_sw_sr");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("sr.wav");
        let mut w = create_streaming_wav(&p, 44100).unwrap();
        w.finalize().unwrap();
        let d = std::fs::read(&p).unwrap();
        assert_eq!(u32::from_le_bytes([d[24], d[25], d[26], d[27]]), 44100);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_streaming_wav_empty_finalize() {
        let dir = std::env::temp_dir().join("memecho_t_sw_emp");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("emp.wav");
        let mut w = create_streaming_wav(&p, 16000).unwrap();
        w.finalize().unwrap();
        let d = std::fs::read(&p).unwrap();
        assert_eq!(d.len(), 44);
        assert_eq!(u32::from_le_bytes([d[40], d[41], d[42], d[43]]), 0);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_flush_safe_returns_bytes() {
        let dir = std::env::temp_dir().join("memecho_t_sw_flush");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("fl.wav");
        let mut w = create_streaming_wav(&p, 16000).unwrap();

        let c: Vec<u8> = vec![0u8; 200];
        w.append(&c).unwrap();
        assert_eq!(w.flushed_bytes(), 0);

        let confirmed = w.flush_safe().unwrap();
        assert_eq!(confirmed, 200);
        assert_eq!(w.flushed_bytes(), 200);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_truncate_and_fixup_wav() {
        let dir = std::env::temp_dir().join("memecho_t_sw_trunc");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("t.wav");

        let mut w = create_streaming_wav(&p, 16000).unwrap();
        w.append(&vec![0xABu8; 200]).unwrap();
        w.finalize().unwrap();

        let d = std::fs::read(&p).unwrap();
        assert_eq!(d.len(), 244);

        truncate_and_fixup_wav(&p, 100).unwrap();
        let d = std::fs::read(&p).unwrap();
        assert_eq!(d.len(), 144);
        assert_eq!(u32::from_le_bytes([d[40], d[41], d[42], d[43]]), 100);
        assert_eq!(u32::from_le_bytes([d[4], d[5], d[6], d[7]]), 36 + 100);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_truncate_wav_to_zero() {
        let dir = std::env::temp_dir().join("memecho_t_sw_trz");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("z.wav");
        let mut w = create_streaming_wav(&p, 16000).unwrap();
        w.append(&vec![0xFFu8; 100]).unwrap();
        w.finalize().unwrap();
        truncate_and_fixup_wav(&p, 0).unwrap();
        let d = std::fs::read(&p).unwrap();
        assert_eq!(d.len(), 44);
        assert_eq!(u32::from_le_bytes([d[40], d[41], d[42], d[43]]), 0);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_truncate_larger_than_file() {
        let dir = std::env::temp_dir().join("memecho_t_sw_trlg");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("l.wav");
        let mut w = create_streaming_wav(&p, 16000).unwrap();
        w.append(&vec![0x42u8; 50]).unwrap();
        w.finalize().unwrap();
        truncate_and_fixup_wav(&p, 999).unwrap();
        let d = std::fs::read(&p).unwrap();
        assert_eq!(d.len(), 94);
        assert_eq!(u32::from_le_bytes([d[40], d[41], d[42], d[43]]), 50);
        std::fs::remove_dir_all(&dir).ok();
    }
}
