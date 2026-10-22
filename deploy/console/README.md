# Admin console static files

nginx serves this directory read-only at `https://<PUBLIC_HOSTNAME>/console/` (mounted at `/srv/www/console`).
It is empty until the Flutter web build is copied here (everything except this file and `.gitkeep` is git-ignored):

    cd frontend
    flutter build web --release --target lib/main_console.dart --base-href /console/ --no-web-resources-cdn --pwa-strategy none
    rm -rf ../deploy/console/* && cp -r build/web/. ../deploy/console/    # keep README.md/.gitkeep if you wipe by hand

`index.html` must be directly in this directory. No restart is needed: nginx revalidates files on each load.
CSP is strict (no CDN, no inline script); the build must not reference external hosts.
