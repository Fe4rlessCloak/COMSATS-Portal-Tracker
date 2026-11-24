#!/usr/bin/env bash
# Pull the published tracker image, recreate the container only when the image
# actually changed, then delete superseded tracker images.
#
# Run by comsats-tracker-update.service (oneshot) on a 15-minute timer. Safe to
# run by hand: `--dry-run` prints what it would do and changes nothing.
#
# Why the image ID and not the tag
# ------------------------------
# `:latest` always reads ":latest" whether or not a new image was published, so
# the tag cannot answer "did anything change". The ID the tag resolves to --
# `docker image inspect --format '{{.Id}}'` -- can, and comparing it with the ID
# the running container was created from is what keeps an idle host from
# restarting a healthy tracker every fifteen minutes.
#
# Why the prune is label-scoped
# -----------------------------
# This host runs other people's containers (media servers, download clients, and
# so on). `docker image prune` cannot filter labels at all, and pruning by name
# or tag prefix would match whatever else happens to be named similarly. So the
# only images this script will ever consider deleting are the ones carrying
#
#   org.opencontainers.image.source=https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker
#
# -- a label only .github/workflows/publish-image.yml attaches. That scope exists
# precisely so unrelated host services stay untouched.
#
# Nothing is deleted if a container is still using it: `docker image rm` without
# `--force` refuses in-use images, and this script never forces. A refused
# removal is a warning, not a failure; the next run retries.
#
# Why the opt-in profile is detected rather than always passed
# -----------------------------------------------------------
# `session-ui` is a Compose *profile*: plain `docker compose pull` and
# `docker compose up -d` act on the default profile only and are documented to
# leave profiled services alone. A host that ran
# `docker compose --profile session-ui up -d` would therefore keep the previous
# image in `comsats-session-ui` forever while the tracker moved to the new one
# -- the two containers would share a tag but run different builds, silently.
# Passing `--profile session-ui` unconditionally would fix that by *starting a
# service the operator never enabled*, which is worse: the default install must
# stay tracker-only.
#
# So the script asks the daemon whether that container exists at all
# (`docker inspect`, which fails when it does not) and adds the profile to both
# the pull and the up only when it does. A tracker-only host's Compose
# invocations stay byte-identical, and an enabled host's UI is updated in the
# same run that updates the tracker. A container that exists but was stopped by
# hand still counts as enabled -- otherwise stopping the page once would strand
# it on a stale image.

set -euo pipefail

IMAGE="ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest"
CONTAINER="comsats-portal-tracker"
# The opt-in service, as declared in docker-compose.yml. Different name on
# purpose: `docker inspect` must be able to tell the two apart.
SESSION_UI_CONTAINER="comsats-session-ui"
SESSION_UI_PROFILE="session-ui"
SOURCE_LABEL="org.opencontainers.image.source=https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker"
PRUNE_FILTER="label=${SOURCE_LABEL}"

# The stack root is two levels above this script (deploy/systemd/), so the units
# can live anywhere and no absolute host path has to be committed.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${STACK_DIR:-$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-${STACK_DIR}/docker-compose.yml}"
DOCKER="${DOCKER:-docker}"

DRY_RUN=0
for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            printf '%s\n' \
                "Usage: comsats-tracker-update.sh [--dry-run]" \
                "" \
                "Pull ${IMAGE}, recreate ${CONTAINER} only if the image ID changed," \
                "then delete superseded images labelled with this project's" \
                "org.opencontainers.image.source. Other images are never touched." \
                "" \
                "  --dry-run   report what would change; pull, recreate and delete nothing"
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$argument" >&2
            exit 2
            ;;
    esac
done

log() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

compose() {
    "$DOCKER" compose --project-directory "$STACK_DIR" -f "$COMPOSE_FILE" \
        ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} "$@"
}

# `--profile` is a global Compose flag, so it has to sit with `-f` and before
# the subcommand -- hence the array threaded through `compose()` rather than a
# suffix on each call. Empty unless the opt-in container exists (see the header).
# Reading `{{.Image}}` in this same inspect call lets us check whether the UI
# container is stale without a second `docker inspect` invocation.
PROFILE_ARGS=()
UI_RUNNING_ID=""
if UI_RUNNING_ID="$("$DOCKER" inspect --format '{{.Image}}' "$SESSION_UI_CONTAINER" 2>/dev/null)"; then
    PROFILE_ARGS=(--profile "$SESSION_UI_PROFILE")
else
    UI_RUNNING_ID=""
fi

# `docker images -q` is not consistent about the digest algorithm prefix: with
# `--no-trunc` some daemons print the bare hex, while `image inspect {{.Id}}`
# always prints `sha256:...`. Compare on the hex only, so the running image is
# still recognised as "in use" either way.
strip_digest() {
    printf '%s' "${1#sha256:}"
}

# The ID the `:latest` tag points at locally, or empty when it has never been
# pulled. Empty is normal on a first run, not an error.
image_id() {
    "$DOCKER" image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || true
}

# The ID the running container was created from; empty when it does not exist.
container_image_id() {
    "$DOCKER" inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true
}

if [ "$DRY_RUN" -eq 1 ]; then
    log "dry run: no pull, no recreate, no deletion"
    if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
        log "compose scope: default profile + ${SESSION_UI_PROFILE} (${SESSION_UI_CONTAINER} exists)"
    else
        log "compose scope: default profile only (${SESSION_UI_CONTAINER} absent)"
    fi
else
    log "pulling ${IMAGE}"
    compose pull
fi

NEW_ID="$(image_id)"
if [ -z "$NEW_ID" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "${IMAGE} is not present locally yet"
    else
        warn "pull left no local ${IMAGE}; nothing to do"
        exit 1
    fi
fi

NEW_HEX="$(strip_digest "$NEW_ID")"
RUNNING_ID="$(container_image_id)"
RUNNING_HEX="$(strip_digest "$RUNNING_ID")"

# Compare bare hex: the two commands disagree about the `sha256:` prefix.
# Check both the tracker and (when enabled) `comsats-session-ui` so the UI
# container cannot stay stranded on an older image if the tracker already moved.
NEEDS_RECREATE=0
RECREATE_REASON=""
if [ -z "$NEW_ID" ] || [ "$NEW_HEX" != "$RUNNING_HEX" ]; then
    NEEDS_RECREATE=1
    RECREATE_REASON="${CONTAINER} running ${RUNNING_ID:-nothing}"
fi
if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
    UI_HEX="$(strip_digest "$UI_RUNNING_ID")"
    if [ -z "$NEW_ID" ] || [ "$NEW_HEX" != "$UI_HEX" ]; then
        NEEDS_RECREATE=1
        if [ -n "$RECREATE_REASON" ]; then
            RECREATE_REASON="${RECREATE_REASON}; ${SESSION_UI_CONTAINER} running ${UI_RUNNING_ID:-nothing}"
        else
            RECREATE_REASON="${SESSION_UI_CONTAINER} running ${UI_RUNNING_ID:-nothing}"
        fi
    fi
fi

if [ "$NEEDS_RECREATE" -eq 0 ]; then
    if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
        log "unchanged: ${CONTAINER} and ${SESSION_UI_CONTAINER} already run ${NEW_ID}"
    else
        log "unchanged: ${CONTAINER} already runs ${NEW_ID}"
    fi
elif [ "$DRY_RUN" -eq 1 ]; then
    log "would recreate (${RECREATE_REASON}; image ${NEW_ID})"
else
    log "image changed (${RECREATE_REASON}; image ${NEW_ID}); recreating"
    compose up -d
fi

# Candidates: only images carrying this project's source label. Excluded from
# deletion: the image the tag now points at, which is what the container runs
# once `up` above finished -- it is both "the currently-running image" and
# "the latest tag".
mapfile -t LABELLED < <("$DOCKER" images --filter "$PRUNE_FILTER" --no-trunc -q 2>/dev/null || true)

# One image carries several tags, so it is listed once per tag. Removing it
# twice would make the second call fail for no reason, so keep the first
# occurrence of each ID.
declare -A SEEN=()

removed=0
for candidate in "${LABELLED[@]:-}"; do
    [ -n "$candidate" ] || continue
    key="$(strip_digest "$candidate")"
    if [ -n "${SEEN[$key]:-}" ]; then
        continue
    fi
    SEEN[$key]=1
    if [ "$(strip_digest "$candidate")" = "$(strip_digest "$NEW_ID")" ]; then
        continue
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log "would remove ${candidate}"
        continue
    fi
    if "$DOCKER" image rm "$candidate" >/dev/null 2>&1; then
        log "removed superseded image ${candidate}"
        removed=$((removed + 1))
    else
        # Almost always "image is being used by a running container": leave it,
        # it is not this script's business to force.
        warn "kept ${candidate} (still in use or already gone)"
    fi
done

if [ "$DRY_RUN" -eq 0 ]; then
    log "prune complete: ${removed} superseded image(s) removed"
fi
