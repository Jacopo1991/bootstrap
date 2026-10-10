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
printf '.work/\n' > "$repo/.gitignore"
printf 'tracked working tree content\n' > "$repo/tracked.txt"
git -C "$repo" add .gitignore tracked.txt
git -C "$repo" commit -qm fixture
printf 'ignored lane scratch\n' > "$repo/.work/lane.log"
cat > "$tmp/gitleaks" <<'SCANNER'
#!/usr/bin/env bash
set -euo pipefail
[[ $1 == dir && $2 == --redact && $3 == --no-banner ]]
[[ -f $4/tracked.txt && -f $4/.gitignore ]]
[[ ! -e $4/.work ]]
echo 'working-tree scan excludes ignored .work and includes tracked files'
SCANNER
chmod 0755 "$tmp/gitleaks"
bash "$ROOT/ci/gitleaks-working-tree.sh" "$tmp/gitleaks" "$repo" "$tmp/scan"
grep -Fq '"$tmp/gitleaks" git --redact --no-banner .' "$ROOT/ci/lint.sh"
echo 'PASS: gitleaks git remains enabled for tracked history'
