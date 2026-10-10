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
[[ $1 == dir ]]
shift
config=
while (($# > 1)); do
  case "$1" in
    --redact|--no-banner) shift ;;
    --config) config=$2; shift 2 ;;
    *) exit 2 ;;
  esac
done
scan_root=$1
[[ -f $scan_root/tracked.txt && -f $scan_root/.gitignore && -f $scan_root/untracked.txt ]]
[[ -f $scan_root/.work/tracked.log && ! -e $scan_root/.work/lane.log ]]
[[ -f $scan_root/local.env ]]
if [[ -f $scan_root/allowlisted.txt ]]; then
  [[ $config == "$FIXTURE_REPO/.gitleaks.toml" ]]
  grep -Fq 'FIXTURE_SECRET_ALLOWED' "$config"
  grep -Fq 'FIXTURE_SECRET_ALLOWED' "$scan_root/allowlisted.txt"
else
  [[ -z $config ]]
fi
if grep -R -Fq 'FIXTURE_SECRET_UNALLOWLISTED' "$scan_root"; then
  echo 'mock gitleaks: unallowlisted secret detected' >&2
  exit 1
fi
echo 'mock gitleaks: no unallowlisted secret found'
SCANNER
chmod 0755 "$tmp/gitleaks"
bash "$ROOT/ci/gitleaks-working-tree.sh" "$tmp/gitleaks" "$repo" "$tmp/scan"
printf '[allowlists]\nregexes = ["FIXTURE_SECRET_ALLOWED"]\n' > "$repo/.gitleaks.toml"
printf 'FIXTURE_SECRET_ALLOWED\n' > "$repo/allowlisted.txt"
git -C "$repo" add .gitleaks.toml allowlisted.txt
git -C "$repo" commit -qm 'fixture gitleaks allowlist'
FIXTURE_REPO=$repo bash "$ROOT/ci/gitleaks-working-tree.sh" "$tmp/gitleaks" "$repo" "$tmp/allowlisted-scan"
printf 'FIXTURE_SECRET_UNALLOWLISTED\n' > "$repo/unallowlisted.txt"
if FIXTURE_REPO=$repo bash "$ROOT/ci/gitleaks-working-tree.sh" "$tmp/gitleaks" "$repo" "$tmp/unallowlisted-scan"; then
  echo 'FAIL: unallowlisted secret unexpectedly passed' >&2
  exit 1
fi
echo 'PASS: repository allowlist is applied and unallowlisted secrets still fail'
grep -Fq "\"\$tmp/gitleaks\" git --redact --no-banner ." "$ROOT/ci/lint.sh"
echo 'PASS: gitleaks git remains enabled for tracked history'
