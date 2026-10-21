#!/bin/bash

base=/home/torbenh/cvs/mount-test/
testdir=$(mktemp -d "$base/overlay-test.XXXXXX")
mkdir "$testdir"/{lower,upper,work,merged}
printf 'lower\n' > "$testdir/lower/check"

unshare --user --map-root-user --mount sh -eu -c '
    mount --make-rprivate /
    mount -t overlay overlay \
      -o "lowerdir=$1/lower,upperdir=$1/upper,workdir=$1/work" \
      "$1/merged"
    printf "upper\n" > "$1/merged/check"
    test "$(cat "$1/merged/check")" = upper
    test "$(cat "$1/lower/check")" = lower
    test -f "$1/upper/check"
    umount "$1/merged"
' sh "$testdir"
result=$?

rm -rf "$testdir"
if [ "$result" -eq 0 ]; then
    echo "Unprivileged OverlayFS works on $base"
else
    echo "Unprivileged OverlayFS test failed (exit $result)"
fi
