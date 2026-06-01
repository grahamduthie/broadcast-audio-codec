# Development Workflow

This document describes how to develop features on the live PSA300 and push them to the public GitHub repository while maintaining sanitization.

## Setup

### Local Mac repo
- Public, sanitized version of the code
- Ready to push to GitHub
- Contains `briclite/config.example.json` (template, no real IPs)
- `.gitignore` excludes `briclite/config.json` (so real config never lands here)

### PSA300 live repo
- Git repo initialized at `/opt/briclite`
- Tracks the running code with real IP addresses and credentials
- `briclite/config.json` is in `.gitignore` (never committed)
- Venv and cache are in `.gitignore` (keeps history clean)
- Two commits:
  1. Initial state snapshot
  2. Cleanup of venv/cache

## Workflow: Developing a New Feature

### 1. Develop on PSA300

Make your changes directly on the running unit:

```bash
ssh marlowfm@172.16.10.213

# Edit files
nano /opt/briclite/briclite/main.py
nano /opt/briclite/briclite/core/pipeline_manager.py

# Test the changes
sudo systemctl restart briclite
curl http://localhost:8080/api/connect
# Verify the feature works...

# Commit to local repo
cd /opt/briclite
git add briclite/main.py briclite/core/pipeline_manager.py
git commit -m "Add feature X

Description of what this does and why."
```

**Important:** Do NOT commit `config.json`. It's in `.gitignore` and will be rejected.

### 2. Pull changes to Mac

```bash
cd /Users/gduthie/Programming/Codec

# Copy the modified files from PSA300
scp marlowfm@172.16.10.213:/opt/briclite/briclite/main.py briclite/
scp marlowfm@172.16.10.213:/opt/briclite/briclite/core/pipeline_manager.py briclite/core/

# Or clone/pull if you set up a git remote on PSA300
```

### 3. Verify sanitization

The Mac repo should already have sanitized examples. If your feature references any IPs, usernames, or service names, replace them:

```bash
# Example: if you added a default IP
sed -i '' 's/217\.36\.229\.106/192.0.2.100/g' briclite/main.py

# Commit locally
git add briclite/
git commit -m "Add feature X (from PSA300)"
```

### 4. Push to GitHub

```bash
git push origin main
```

## Quick Reference

| Location | Purpose | Contains | Config |
|---|---|---|---|
| Mac `/Users/gduthie/Programming/Codec` | Public repo | Sanitized code, examples | `config.example.json` |
| PSA300 `/opt/briclite` | Live repo | Running code, real data | `config.json` (ignored) |

## Keeping in Sync

After pulling from PSA300, the two repos will diverge slightly:
- PSA300 repo has `config.json` tracked in git history (sanitized to example values)
- Mac repo's `config.json` is in `.gitignore`

This is intentional and safe. The Mac repo is the "clean" version; the PSA300 repo is the "working" version.

If you want to see what the PSA300 repo looks like, check its commits:

```bash
ssh marlowfm@172.16.10.213 "cd /opt/briclite && git log --oneline"
```

## Tips

- Always test features on the PSA300 before committing
- Use descriptive commit messages — they help others understand what changed
- Don't manually edit `config.json` in the Mac repo; keep it in `.gitignore`
- If a feature requires config changes, update `config.example.json` and document the new field in BUILD.md

## Future: Automated Sync

As the project matures, you could set up:
- A git remote on PSA300 that the Mac repo can pull from
- A script to automatically sanitize when pulling from PSA300
- A CI/CD pipeline to test changes before they land in GitHub

For now, manual pull-and-commit is straightforward and keeps you in control of what gets published.
