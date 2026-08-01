/// Windows Credential Manager integration.
///
/// Stores and retrieves credentials using the Windows Credential Manager API.
/// No plaintext secrets are written to config files.

#[cfg(windows)]
mod platform {
    use windows::core::PWSTR;
    use windows::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_FLAGS,
        CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };

    const TARGET_PREFIX: &str = "memecho:";

    fn make_target(name: &str) -> Vec<u16> {
        let full = format!("{}{}", TARGET_PREFIX, name);
        let mut wide: Vec<u16> = full.encode_utf16().collect();
        wide.push(0); // null terminator
        wide
    }

    pub fn credential_set(name: &str, secret: &str) -> Result<(), CredError> {
        let target = make_target(name);
        let user: Vec<u16> = "memecho\0".encode_utf16().collect();
        let mut secret_bytes: Vec<u8> = secret.as_bytes().to_vec();

        let cred = CREDENTIALW {
            Flags: CRED_FLAGS(0),
            Type: CRED_TYPE_GENERIC,
            TargetName: PWSTR(target.as_ptr() as *mut _),
            Comment: PWSTR::null(),
            LastWritten: Default::default(),
            CredentialBlobSize: secret_bytes.len() as u32,
            CredentialBlob: secret_bytes.as_mut_ptr(),
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount: 0,
            Attributes: std::ptr::null_mut(),
            TargetAlias: PWSTR::null(),
            UserName: PWSTR(user.as_ptr() as *mut _),
        };

        unsafe {
            CredWriteW(&cred, 0).map_err(|e| CredError::Write(e.to_string()))?;
        }
        Ok(())
    }

    pub fn credential_get(name: &str) -> Result<String, CredError> {
        let target = make_target(name);
        unsafe {
            let mut cred_ptr: *mut CREDENTIALW = std::ptr::null_mut();
            let result = CredReadW(
                PWSTR(target.as_ptr() as *mut _),
                CRED_TYPE_GENERIC,
                Some(0),
                &mut cred_ptr,
            );
            if result.is_err() {
                return Err(CredError::NotFound);
            }
            let cred = &*cred_ptr;
            let blob =
                std::slice::from_raw_parts(cred.CredentialBlob, cred.CredentialBlobSize as usize);
            let secret =
                String::from_utf8(blob.to_vec()).map_err(|e| CredError::Read(e.to_string()))?;
            CredFree(cred_ptr as *const _);
            Ok(secret)
        }
    }

    pub fn credential_delete(name: &str) -> Result<(), CredError> {
        let target = make_target(name);
        unsafe {
            CredDeleteW(PWSTR(target.as_ptr() as *mut _), CRED_TYPE_GENERIC, Some(0))
                .map_err(|_| CredError::NotFound)?;
        }
        Ok(())
    }

    #[derive(Debug, thiserror::Error)]
    pub enum CredError {
        #[error("credential not found")]
        NotFound,
        #[error("failed to write credential: {0}")]
        Write(String),
        #[error("failed to read credential: {0}")]
        Read(String),
    }
}

#[cfg(not(windows))]
mod platform {
    pub fn credential_set(_name: &str, _secret: &str) -> Result<(), CredError> {
        Err(CredError::Unsupported)
    }

    pub fn credential_get(_name: &str) -> Result<String, CredError> {
        Err(CredError::Unsupported)
    }

    pub fn credential_delete(_name: &str) -> Result<(), CredError> {
        Err(CredError::Unsupported)
    }

    #[derive(Debug, thiserror::Error)]
    pub enum CredError {
        #[error("credential not found")]
        NotFound,
        #[error("failed to write credential: {0}")]
        Write(String),
        #[error("failed to read credential: {0}")]
        Read(String),
        #[error("credential manager not supported on this platform")]
        Unsupported,
    }
}

pub use platform::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_credential_roundtrip() {
        if cfg!(windows) {
            let test_name = "memecho_test_roundtrip";
            let test_secret = "test-secret-value-12345";

            let result = credential_set(test_name, test_secret);
            if result.is_ok() {
                let retrieved = credential_get(test_name).unwrap();
                assert_eq!(retrieved, test_secret);

                credential_delete(test_name).unwrap();

                let result = credential_get(test_name);
                assert!(result.is_err());
            }
        }
    }

    #[test]
    fn test_credential_not_found() {
        if cfg!(windows) {
            let result = credential_get("memecho_nonexistent_key_xyz");
            assert!(result.is_err());
        }
    }

    #[test]
    fn test_credential_delete_nonexistent() {
        if cfg!(windows) {
            let result = credential_delete("memecho_nonexistent_key_xyz");
            assert!(result.is_err());
        }
    }
}
