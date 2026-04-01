#!/bin/bash
# Wrapper to run FidelityFX_SC.exe under Wine on Linux.
# Post-processes depfiles to strip Wine's Z: drive prefix.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wine "${SCRIPT_DIR}/FidelityFX_SC.exe" "$@"
RET=$?

# Find and fix any .d depfiles in the output directory — Wine generates
# paths like Z:/home/... which Make can't parse (: is a pattern separator).
for arg in "$@"; do
    if [[ "$arg" == -output=* ]]; then
        OUTDIR="${arg#-output=}"
        if [ -d "$OUTDIR" ]; then
            for depfile in "$OUTDIR"/*.d; do
                [ -f "$depfile" ] && sed -i 's|Z:/|/|g' "$depfile"
            done
        fi
        break
    fi
done

exit $RET
