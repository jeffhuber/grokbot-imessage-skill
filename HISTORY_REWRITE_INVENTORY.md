# Git History Rewrite Inventory

## Purpose
Scrub personal information from git history per Batch C item 12:
- Replace "Angel Vossough" references with "Alex Example" in all commits
- Standardize commit author email from personal to GitHub noreply

## Current State Analysis

### 1. Content References to "Angel Vossough"

Found in multiple historical commits (pre-PR #39):

**Commits containing "Angel Vossough":**
- Multiple commits from initial repo creation through early development
- Present in documentation, examples, and code comments

**Files affected in historical commits:**
- `SKILL.md` - Example queries and parameters
- `bin/helper.py` - Code comments
- `docs/PROTOCOL.md` - Protocol examples and documentation
- `README.md` - Usage examples

**Note:** PR #39 already replaced these in the working tree, but they remained
in git history before that commit.

### 2. Commit Author Email

**Personal email in commit metadata:**
- Email: jhuber@gmail.com
- Found in: ~25 commits throughout history
- Will be replaced with: jeffhuber@users.noreply.github.com

### 3. Intentional Contact Information (PRESERVED)

**SECURITY.md contact email:**
- Email: jhuber+grokbotimessage@gmail.com
- Location: SECURITY.md line 310
- Action: **PRESERVED** - This is the intentional security contact
- Rationale: Public vulnerability disclosure contact, meant to be findable

## Planned Replacements

### Content Replacements (in all commits)

1. **"Angel Vossough" → "Alex Example"**
   - Literal string replacement in all file contents
   - Affects documentation, examples, and comments
   - ~10+ instances across multiple commits

### Metadata Replacements

1. **Author Email Standardization**
   - Old: jhuber@gmail.com
   - New: jeffhuber@users.noreply.github.com
   - Affects: ~25 commits
   - Preserves: Author name "Jeff Huber"

## Rewrite Scope

This rewrite includes **all commits through PR #40** (Batch B security fixes).
The rewritten history contains:
- All original commits with scrubbed personal info
- PR #39 (privacy scrub of working tree)
- PR #40 (Batch B security fixes)
- Full commit history up to the rewrite

## Tools and Method

Using `git-filter-repo` with:
1. `--replace-text` for content replacements
2. `--mailmap` for email standardization

## Validation Steps

Before merge:
1. Verify all "Angel Vossough" instances replaced
2. Verify all jhuber@gmail.com instances replaced in metadata
3. Verify SECURITY.md contact email preserved
4. Check commit history integrity (signatures will be lost)
5. Verify working tree matches current main including PR #40

## Risks and Warnings

⚠️ **BREAKING CHANGES - REQUIRES FORCE PUSH**

1. **All commit SHAs will change** - entire history rewritten
2. **Existing clones become invalid** - all collaborators must re-clone
3. **Open PRs become orphaned** - may need rebasing or recreation
4. **Forks become diverged** - forks must rebase or sync manually
5. **Git signatures lost** - GPG signatures will be stripped
6. **CI/CD references** - any systems referencing old SHAs break

## Merge Instructions for Repository Owner

After reviewing this PR:

1. **Backup**: Create a backup branch of current main
   ```bash
   git checkout main
   git branch backup-pre-history-rewrite-$(date +%Y%m%d)
   git push origin backup-pre-history-rewrite-$(date +%Y%m%d)
   ```

2. **Coordinate**: Notify all contributors of impending history rewrite
   - Close or merge all open PRs first
   - Ask contributors to push any uncommitted work

3. **Merge**: DO NOT use GitHub merge button
   ```bash
   git fetch origin cursor/history-scrub-privacy-5726
   git checkout cursor/history-scrub-privacy-5726
   
   # Review the changes
   cat HISTORY_REWRITE_INVENTORY.md
   cat BEFORE_AFTER_EVIDENCE.md
   
   # Verify no personal info remains
   git log --all -S "Angel Vossough" --oneline  # should be empty
   git log --all --pretty=format:"%ae" | grep "jhuber@gmail.com"  # should be empty
   
   # Force-push to main
   git checkout main
   git reset --hard cursor/history-scrub-privacy-5726
   git push --force-with-lease origin main
   ```

4. **Cleanup**: Delete the rewrite branch
   ```bash
   git push origin --delete cursor/history-scrub-privacy-5726
   ```

5. **Notify**: Tell all contributors to:
   ```bash
   # Backup local work
   git stash
   # Nuke local repo and re-clone
   cd ..
   rm -rf grokbot-imessage-skill
   git clone https://github.com/jeffhuber/grokbot-imessage-skill.git
   cd grokbot-imessage-skill
   git stash pop  # if they had local work
   ```

## Alternative: Non-Destructive Approach

If the risks are too high, consider:
- Keep history as-is
- Document the privacy scrub in a PRIVACY.md file
- Add a note to README referencing PR #39
- Accept that git history contains the old references

This avoids all breaking changes but leaves personal info in history.
