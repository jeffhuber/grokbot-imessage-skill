# Shared Core Maintenance

The Claude Cowork, Grok Bot, and ChatGPT/Codex repositories deploy independent
macOS helpers. They intentionally do not share an installed wrapper, LaunchAgent,
bridge directory, read policy, nonce store, log, response queue, or Full Disk
Access identity.

They do share a reviewed security implementation. `shared-core.json` records
SHA-256 fingerprints for the common checker, helper, send gate, native wrapper,
and confirmation helper. Host identity and release version values in `helper.py`
are normalized before hashing; no behavioral code is ignored.

Every repository's CI runs:

```bash
python3 tools/check_shared_core.py
```

The Grok Bot repository also runs a scheduled comparison of all three public
default branches. For a local cross-repository comparison, run:

```bash
python3 /path/to/grokbot-imessage-skill/tools/check_shared_core.py \
  /path/to/claudecowork-imessage-skill \
  /path/to/grokbot-imessage-skill \
  /path/to/chatgpt-codex-imessage-plugin
```

When changing a shared-core file:

1. Apply the behavioral change and its tests to all affected repositories.
2. Review host-specific identity substitutions in each `shared-core.json`.
3. Run each repository's tests and the cross-repository comparison.
4. Use `--print` to inspect new fingerprints, then update all three manifests
   together only after reviewing the actual source diff.
5. Land the repository with the scheduled cross-repository workflow last, so
   its first default-branch run observes the other two updates.

The manifest is a drift detector, not a source generator. Runtime isolation
remains mandatory even when the source fingerprints match.
