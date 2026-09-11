# Batch B Security Fixes

## Fixed Issues

### 1. Database Temp File Security (HIGH)
**Issue**: `copy_chatdb()` created temporary database snapshots in the system temp directory with mode 0600. While the file itself was not world-readable, any same-UID process could list the temp directory, discover the temp file name, and read it.

**Fix**: 
- Create a private temp directory with mode 0700 first
- Create the temp database file inside that private directory
- Clean up both the file and directory on exit

**Files Changed**:
- `bin/helper.py`: Modified `copy_chatdb()` and `cleanup_tmpdb()`
- `SECURITY.md`: Updated documentation

### 2. SECURITY.md Vulnerability Reporting
**Status**: ✅ Already compliant
- Email contact present: `jhuber+grokbotimessage@gmail.com`
- GitHub Security Advisories reference included
- No changes needed

### 3. Screenshot Reference
**Issue**: README.md referenced `docs/images/send-confirm-dialog.png` which did not exist, only a placeholder README.

**Fix**: Replaced image reference with descriptive text and HTML comment indicating illustrative nature.

**Files Changed**:
- `README.md`

### 4. Stale Branches
**Deleted**: 5 remote branches that were squash-merged into main:
- `cursor/scaffold-grok-bot-repo-f936` (initial scaffold, incorporated as commit 2a1369b)
- `codex/grok-coexistence-identity` (incorporated as PR #7)
- `cursor/lnch-1-readme-mac-app-link-5b3c` (minor README fix)
- `codex/v1.2.2-announcement` (release branch)
- `codex/fix-merged-allowlist-regression` (fix incorporated)

**Retained**: 4 branches retained as potentially active:
- `codex/harden-runtime-and-requests`
- `codex/hardened-architecture`
- `codex/reliability-onboarding`
- `codex/safety-privacy`

## Batch C LOW Findings

### 5. Typed Identifier Matching (LOW - FIXED)
**Issue**: `_matches_list()` used substring matching for group chat IDs (`lowered in cid.lower()`), causing "chat123" to incorrectly match "chat1234567890".

**Fix**: Changed to exact equality matching for group chat IDs and emails. Phone numbers continue to use last-10-digit matching.

**Files Changed**:
- `bin/helper.py`: Modified `_matches_list()`

### 6. Allowlist Sudo Race (LOW - FIXED)
**Issue**: TOCTOU race in `configure_allowlist.py` between path validation (line 62) and sudo write (line 70). An attacker could replace the allowlist path with a symlink during this window, causing sudo to write to an unintended location.

**Fix**: 
- Resolve path to canonical form before and after sudo operation
- Verify the installed file is at the expected canonical location
- Reject symlinks explicitly

**Files Changed**:
- `tools/configure_allowlist.py`: Modified `install_entries()`

### 7. Post-Approve Body Race (LOW - NOT EXPLOITABLE)
**Investigated**: Examined whether the message body could be swapped between user confirmation and actual send.

**Analysis**: 
- The `text` variable is a local Python variable throughout the send flow
- Same variable is: validated → nonce-checked → shown in dialog → written to temp file → sent
- Python's execution model prevents external modification of local variables
- No file or shared state is accessed after confirmation

**Conclusion**: Not exploitable. No fix needed.

## Summary
- **Batch B items**: 4/4 complete (1 fix, 1 verified, 1 fix, 1 cleanup)
- **Batch C LOWs**: 2/3 fixed (typed identifier, sudo race), 1 confirmed not exploitable (body race)
