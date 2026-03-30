CW Node Helper — CI/CD Pipeline Guide
=======================================

This guide walks you through setting up a real CI/CD pipeline using
GitHub + GitHub Actions. When done, your workflow will be:

  1. Make changes locally
  2. Commit + push (tests run automatically)
  3. Tag a version → zip is built + published as a GitHub Release
  4. Teammates run `bash update.sh` → pulls latest release automatically

No more manual zipping or Google Drive uploads.


STEP 1 — Initialize Git Repo
-----------------------------
You already have a .gitignore. Initialize the repo:

  cd ~/Documents/Random/cw-node-helper
  git init
  git add -A
  git commit -m "Initial commit — v6.2.2"


STEP 2 — Create a GitHub Repo
------------------------------
Option A: GitHub CLI (if you have `gh` installed):

  gh repo create YOUR_ORG/cw-node-helper --private --source=. --push

Option B: Manual:
  1. Go to https://github.com/new (or your your organization GitHub org)
  2. Create a PRIVATE repo called "cw-node-helper"
  3. Don't add README or .gitignore (you already have them)
  4. Copy the remote URL and run:

       git remote add origin git@github.com:YOUR_ORG/cw-node-helper.git
       git push -u origin main


STEP 3 — Add Pre-commit Hook (local test gate)
-----------------------------------------------
This runs tests BEFORE every commit. Broken code never gets committed.

Create the hook file:

  cat > .git/hooks/pre-commit << 'EOF'
  #!/bin/bash
  echo "Running tests..."
  python3 test_integrity.py
  if [ $? -ne 0 ]; then
      echo "Tests failed. Commit aborted."
      exit 1
  fi
  echo "Tests passed ✓"
  EOF
  chmod +x .git/hooks/pre-commit

Now every `git commit` will run your test suite first.


STEP 4 — GitHub Actions Workflow
---------------------------------
Create the workflow directory:

  mkdir -p .github/workflows

Create .github/workflows/ci.yml with this content:

  ┌──────────────────────────────────────────────────────────────┐
  │  name: CI                                                    │
  │                                                              │
  │  on:                                                         │
  │    push:                                                     │
  │      branches: [main]                                        │
  │    pull_request:                                             │
  │                                                              │
  │  jobs:                                                       │
  │    test:                                                     │
  │      runs-on: ubuntu-latest                                  │
  │      strategy:                                               │
  │        matrix:                                               │
  │          python-version: ['3.9', '3.11', '3.13']             │
  │                                                              │
  │      steps:                                                  │
  │        - uses: actions/checkout@v4                            │
  │                                                              │
  │        - uses: actions/setup-python@v6                        │
  │          with:                                               │
  │            python-version: ${{ matrix.python-version }}       │
  │            cache: pip                                         │
  │                                                              │
  │        - run: pip install requests                            │
  │                                                              │
  │        - name: Run tests                                     │
  │          run: python3 test_integrity.py                       │
  └──────────────────────────────────────────────────────────────┘

This runs your test suite on Python 3.9 (Rene's Mac), 3.11, and 3.13
every time you push to main or open a PR.


STEP 5 — Release Workflow (auto-zip on version tag)
----------------------------------------------------
Create .github/workflows/release.yml:

  ┌──────────────────────────────────────────────────────────────┐
  │  name: Release                                               │
  │                                                              │
  │  on:                                                         │
  │    push:                                                     │
  │      tags:                                                   │
  │        - 'v*'                                                │
  │                                                              │
  │  jobs:                                                       │
  │    test:                                                     │
  │      runs-on: ubuntu-latest                                  │
  │      steps:                                                  │
  │        - uses: actions/checkout@v4                            │
  │        - uses: actions/setup-python@v6                        │
  │          with:                                               │
  │            python-version: '3.13'                             │
  │            cache: pip                                         │
  │        - run: pip install requests                            │
  │        - run: python3 test_integrity.py                       │
  │                                                              │
  │    release:                                                  │
  │      needs: test                                             │
  │      runs-on: ubuntu-latest                                  │
  │      permissions:                                            │
  │        contents: write                                       │
  │                                                              │
  │      steps:                                                  │
  │        - uses: actions/checkout@v4                            │
  │                                                              │
  │        - name: Build release zip                             │
  │          run: |                                              │
  │            mkdir -p release/cw-node-helper                   │
  │            cp get_node_context.py release/cw-node-helper/    │
  │            cp ib_topology.json release/cw-node-helper/       │
  │            cp dh_layouts.json release/cw-node-helper/        │
  │            cp load_env.sh release/cw-node-helper/            │
  │            cp update.sh release/cw-node-helper/              │
  │            cp .env.example release/cw-node-helper/           │
  │            cp ENV_GUIDE.txt release/cw-node-helper/          │
  │            cp SETUP.txt release/cw-node-helper/              │
  │            cp requirements.txt release/cw-node-helper/       │
  │            cp test_integrity.py release/cw-node-helper/      │
  │            cp .gitignore release/cw-node-helper/             │
  │            cp -r site release/cw-node-helper/                │
  │            cd release && zip -r cw-node-helper.zip           │
  │              cw-node-helper/                                 │
  │                                                              │
  │        - name: Create GitHub Release                         │
  │          uses: ncipollo/release-action@v1                    │
  │          with:                                               │
  │            artifacts: release/cw-node-helper.zip             │
  │            generateReleaseNotes: true                        │
  └──────────────────────────────────────────────────────────────┘

What this does:
  - Triggered when you push a tag like v6.2.2
  - Runs tests first (release is blocked if tests fail)
  - Builds a clean zip (no .env, no source/, no __pycache__)
  - Publishes it as a GitHub Release with auto-generated changelog


STEP 6 — How to Release a New Version
--------------------------------------
Your new release workflow:

  1. Make your changes
  2. Update APP_VERSION in get_node_context.py
  3. Commit:

       git add -A
       git commit -m "feat: add refresh button and stale export"

  4. Tag it:

       git tag v6.2.2

  5. Push everything:

       git push origin main --tags

  That's it. GitHub Actions will:
    ✓ Run tests on 3 Python versions
    ✓ Build the zip
    ✓ Publish the release
    ✓ Auto-generate a changelog from your commits

  You can see the release at:
    https://github.com/YOUR_ORG/cw-node-helper/releases/latest


STEP 7 — Update the Auto-Updater
----------------------------------
Once releases are on GitHub, rewrite update.sh to pull from there
instead of ~/Downloads. Two options:

Option A — Using `gh` CLI (recommended if teammates have it):

  REPO="YOUR_ORG/cw-node-helper"
  gh release download latest --repo "$REPO" --pattern "cw-node-helper.zip" --dir "$TEMP_DIR"

Option B — Using curl with a token (works everywhere):

  REPO="YOUR_ORG/cw-node-helper"
  TOKEN="$GITHUB_TOKEN"  # or hardcode a read-only PAT
  LATEST=$(curl -sH "Authorization: token $TOKEN" \
    "https://api.github.com/repos/$REPO/releases/latest" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['assets'][0]['browser_download_url'])")
  curl -sLH "Authorization: token $TOKEN" -o "$TEMP_DIR/cw-node-helper.zip" "$LATEST"

For a PRIVATE repo, teammates need either:
  - `gh auth login` (one-time, uses their GitHub account)
  - A read-only Personal Access Token stored in .env as GITHUB_TOKEN

For a PUBLIC repo: no auth needed at all. curl just works.


STEP 8 — .gitignore Additions
------------------------------
Make sure these are in your .gitignore:

  .env
  .cwhelper_state.json
  __pycache__/
  *.pyc
  source/
  .DS_Store


FULL WORKFLOW SUMMARY
=====================

  Developer (you):
    edit code → commit → push → tag → GitHub builds + publishes release

  Teammate:
    bash update.sh → downloads latest release → preserves .env → done

  What runs automatically:
    ✓ Tests on every push (Python 3.9, 3.11, 3.13)
    ✓ Tests again before any release
    ✓ Zip built with only distribution files (no secrets, no source/)
    ✓ Release published with changelog
    ✓ Version number tracked in git tags

  What you never do again:
    ✗ Manual zip
    ✗ Upload to Google Drive
    ✗ Tell people to download from Drive
    ✗ Wonder if tests were run before distributing


QUICK START CHECKLIST
=====================

  [ ] git init
  [ ] Create GitHub repo (private)
  [ ] git remote add origin <url>
  [ ] Create .git/hooks/pre-commit (test gate)
  [ ] Create .github/workflows/ci.yml
  [ ] Create .github/workflows/release.yml
  [ ] git add -A && git commit -m "Initial commit"
  [ ] git push -u origin main
  [ ] git tag v6.2.2 && git push origin --tags
  [ ] Verify: check GitHub Actions tab for green checkmarks
  [ ] Verify: check Releases page for the zip
  [ ] Update update.sh to pull from GitHub Releases
  [ ] Tell teammates: `gh auth login` once, then `bash update.sh`


TROUBLESHOOTING
===============

"Tests fail in CI but pass locally"
  → Check Python version. CI tests on 3.9/3.11/3.13.
  → Check if test needs .env vars (CI doesn't have your tokens).
  → Mock external API calls in tests.

"Release action fails with 403"
  → Add `permissions: contents: write` to the release job.

"Teammates can't download from private repo"
  → They need `gh auth login` or a GITHUB_TOKEN in their .env.
  → Simpler: make the repo public (it has no secrets in it).

"I want to skip tests for a quick fix"
  → Don't. Fix the tests. That's the whole point of CI.
  → If truly urgent: push directly, but tag AFTER tests pass.
