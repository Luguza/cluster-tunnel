#!/usr/bin/env bash
# facts.sh — collects the facts of a just-submitted Slurm job for its ctun job
# record. Prints `key=value` lines on stdout; a fact it cannot read is left out,
# so the caller records it as unknown:
#
#   slurm_cluster=<ClusterName from slurm.conf>
#   job_name=<job name>
#   workdir=<the job's working directory>
#   commit=<HEAD of the git repo in workdir>
#   branch=<its branch, or HEAD when detached>
#   pushed=yes|no            (HEAD is on a remote-tracking branch, as of the last fetch)
#   dirty=<porcelain line>   (one per uncommitted file)
#
# The facts describe the code at submit time, not when the job starts.
# Invoked by ctun as `bash -s -- <job_id>`.
set -u
export LC_ALL=C

job_id="${1:?job id required}"

cluster="$(scontrol show config 2>/dev/null | sed -n 's/^ClusterName *= *//p' | head -n 1)"
[ -n "$cluster" ] && echo "slurm_cluster=$cluster"

# squeue first: a job submitted a moment ago is almost always still queued.
name="$(squeue -h -j "$job_id" -o '%j' 2>/dev/null | head -n 1)"
workdir="$(squeue -h -j "$job_id" -o '%Z' 2>/dev/null | head -n 1)"
if [ -z "$workdir" ]; then
    name="$(sacct -n -X -P -j "$job_id" -o JobName 2>/dev/null | head -n 1)"
    workdir="$(sacct -n -X -P -j "$job_id" -o WorkDir 2>/dev/null | head -n 1)"
fi
[ -n "$name" ] && echo "job_name=$name"
[ -n "$workdir" ] || exit 0
echo "workdir=$workdir"

git -C "$workdir" rev-parse --git-dir >/dev/null 2>&1 || exit 0
echo "commit=$(git -C "$workdir" rev-parse HEAD)"
echo "branch=$(git -C "$workdir" rev-parse --abbrev-ref HEAD)"
if [ -n "$(git -C "$workdir" branch -r --contains HEAD 2>/dev/null)" ]; then
    echo "pushed=yes"
else
    echo "pushed=no"
fi
git -C "$workdir" status --porcelain 2>/dev/null | sed 's/^/dirty=/'
