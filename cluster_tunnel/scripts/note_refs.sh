#!/usr/bin/env bash
# note_refs.sh — checks the references in a `ctun run --note` before the run.
# Prints each reference it can find, one per line; the caller treats every
# other one as unknown.
#
#   job:<id>       known to squeue or sacct, or has a ctun job record
#   commit:<hash>  a commit in one of the given git repos
#
# Invoked by ctun as `bash -s -- <repo>... -- <reference>...`.
set -u
export LC_ALL=C

repos=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
    repos+=("$1")
    shift
done
shift || true

cluster="$(scontrol show config 2>/dev/null | sed -n 's/^ClusterName *= *//p' | head -n 1)"
records_dir="$HOME/.ctun/jobs/$cluster"

job_exists() {
    [ -n "$(squeue -h -j "$1" -o '%i' 2>/dev/null)" ] && return 0
    [ -n "$(sacct -n -X -P -j "$1" -o JobID 2>/dev/null)" ] && return 0
    [ -n "$cluster" ] && [ -f "$records_dir/$1.json" ]
}

commit_exists() {
    local repo
    for repo in "${repos[@]}"; do
        git -C "$repo" cat-file -e "$1^{commit}" 2>/dev/null && return 0
    done
    return 1
}

for ref in "$@"; do
    case "$ref" in
        job:*) job_exists "${ref#job:}" && echo "$ref" ;;
        commit:*) commit_exists "${ref#commit:}" && echo "$ref" ;;
    esac
done
exit 0
