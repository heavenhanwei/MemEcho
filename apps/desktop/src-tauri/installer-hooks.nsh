; Stop an older memEcho installation before NSIS replaces the bundled gateway.
; The desktop normally owns and stops its gateway, but builds installed before
; that lifecycle fix can leave orphaned gateway processes behind.
!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Closing an older memEcho instance..."

  ; Restrict cleanup to memEcho's two executable names. taskkill returns a
  ; non-zero status when a process is not running, which is safe to ignore.
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /T /IM "memecho-desktop.exe"'
  Pop $0
  Pop $1
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /T /IM "memecho-gateway.exe"'
  Pop $0
  Pop $1

  ; Give Windows a short moment to release executable image mappings.
  Sleep 750
!macroend
