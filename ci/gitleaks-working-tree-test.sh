#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
repo=$tmp/repo
mkdir -p "$repo/.work"
git -C "$repo" init -q -b main
git -C "$repo" config user.name test
git -C "$repo" config user.email test@example.invalid
printf '.work/\n*.env\n' > "$repo/.gitignore"
printf 'tracked working tree content\n' > "$repo/tracked.txt"
git -C "$repo" add .gitignore tracked.txt
git -C "$repo" commit -qm fixture
printf 'ignored lane scratch\n' > "$repo/.work/lane.log"
printf 'tracked work content\n' > "$repo/.work/tracked.log"
git -C "$repo" add -f .work/tracked.log
git -C "$repo" commit -qm 'track work fixture'
printf 'ignored environment file\n' > "$repo/local.env"
printf 'untracked working tree content\n' > "$repo/untracked.txt"
cat > "$tmp/gitleaks" <<'SCANNER'
#!/usr/bin/env bash
set -euo pipefail
[[ $1 == dir && $2 == --redact && $3 == --no-banner ]]
[[ -f $4/tracked.txt && -f $4/.gitignore && -f $4/untracked.txt ]]
[[ -f $4/.work/tracked.log && ! -e $4/.work/lane.log ]]
[[ -f $4/local.env ]]
echo 'working-tree scan excludes only ignored .work and includes other ignored files'
SCANNER
chmod 0755 "$tmp/gitleaks"
bash "$ROOT/ci/gitleaks-working-tree.sh" "$tmp/gitleaks" "$repo" "$tmp/scan"
grep -Fq "\"\$tmp/gitleaks\" git --redact --no-banner ." "$ROOT/ci/lint.sh"
echo 'PASS: gitleaks git remains enabled for tracked history'
