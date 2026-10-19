#!/usr/bin/env bash
# records.sh — the ctun job-record store on the cluster. One JSON file per job in
# ~/.ctun/jobs/<ClusterName>/<job id>.json, so every machine that submits to this
# cluster finds the same records. The folder is keyed by Slurm's own ClusterName,
# never by a machine's local config name.
#
# Commands (invoked by ctun as `bash -s -- <command> <args...>`):
#
#   write <job_id> <base64 JSON>   store a new record; refuses to overwrite (exit 4)
#   update <job_id> <base64 JSON>  store a record in place of the job's current one,
#                                  or as its first
#   list <since_minutes> [job_id...]
#                                  print the records written in the last
#                                  <since_minutes> minutes (0 = none) and those of
#                                  the given jobs, one JSON document per line
#   all                            print every record, one per line
#
# Only records go to stdout; diagnostics go to stderr.
set -u
export LC_ALL=C

cluster="$(scontrol show config 2>/dev/null | sed -n 's/^ClusterName *= *//p' | head -n 1)"
if [ -z "$cluster" ]; then
    echo "cannot read the Slurm ClusterName (scontrol show config)" >&2
    exit 3
fi
dir="$HOME/.ctun/jobs/$cluster"

print_record() {
    # Records are one line of JSON; make sure each ends its own line.
    cat "$1"
    [ -n "$(tail -c 1 "$1")" ] && echo
    return 0
}

command="${1:-}"
shift || true

# Decodes the base64 record $2 of job $1 into a temp file next to its target and
# prints the temp file's path, so the final `mv` is atomic.
decode_to_temp() {
    mkdir -p "$dir"
    local tmp
    tmp="$(mktemp "$dir/.$1.XXXXXX")"
    if ! printf '%s' "$2" | base64 -d > "$tmp"; then
        rm -f "$tmp"
        echo "the record is not valid base64" >&2
        exit 5
    fi
    echo "$tmp"
}

case "$command" in
    write)
        job_id="${1:?job id required}"
        payload="${2:?record required}"
        target="$dir/$job_id.json"
        if [ -e "$target" ]; then
            echo "a record for job $job_id already exists" >&2
            exit 4
        fi
        tmp="$(decode_to_temp "$job_id" "$payload")" || exit 5
        # mv -n never replaces a record another submission wrote meanwhile.
        mv -n "$tmp" "$target"
        if [ -e "$tmp" ]; then
            rm -f "$tmp"
            echo "a record for job $job_id already exists" >&2
            exit 4
        fi
        ;;
    update)
        job_id="${1:?job id required}"
        payload="${2:?record required}"
        tmp="$(decode_to_temp "$job_id" "$payload")" || exit 5
        mv -f "$tmp" "$dir/$job_id.json"
        ;;
    list)
        since_min="${1:-0}"
        shift || true
        [ -d "$dir" ] || exit 0
        printed=" "
        if [ "$since_min" -gt 0 ] 2>/dev/null; then
            while IFS= read -r file; do
                print_record "$file"
                printed="$printed$(basename "$file" .json) "
            done < <(find "$dir" -maxdepth 1 -name '*.json' -mmin "-$since_min" 2>/dev/null)
        fi
        for job_id in "$@"; do
            case "$printed" in *" $job_id "*) continue ;; esac
            if [ -f "$dir/$job_id.json" ]; then print_record "$dir/$job_id.json"; fi
        done
        ;;
    all)
        [ -d "$dir" ] || exit 0
        for file in "$dir"/*.json; do
            if [ -f "$file" ]; then print_record "$file"; fi
        done
        ;;
    *)
        echo "usage: records.sh write|update <job_id> <base64> | list <since_minutes> [job_id...] | all" >&2
        exit 2
        ;;
esac
