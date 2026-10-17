LuckyPath public demo - deployment guide

LuckyPath compares a personalized walking route with the shortest route in
Williamsburg, Brooklyn. Choose an origin, destination and available time.
Optional Saju and astrology analyses independently produce experimental
experience priorities, which are combined into one route recommendation.
These interpretation rules are not scientifically validated predictions.

Application code: AGPL-3.0-only (see LICENSE). Third-party code and data retain
their own licenses. Birth details are processed in memory; saving a profile is
optional and uses the visitor's browser storage. No account database is included.

Source repository: https://github.com/jk5052/luckypath-demo
Deploy: https://render.com/deploy?repo=https://github.com/jk5052/luckypath-demo

This folder is a source template. Upload the generated luckypath-demo package,
not the complete development workspace. The package includes the prepared
Williamsburg dataset and a separate Flask/Gunicorn server. The original local
server and pilot remain unchanged.

1. GitHub and Render
   Create/select a repository and put the CONTENTS of luckypath-demo at its root.
   On Render, choose New > Blueprint and connect that repository. Select the
   included render.yaml. It declares one free Docker web service and /health.
   Automatic deploys are off, so later Git pushes do not silently publish changes.
   Alternatively: New > Web Service > Docker, Dockerfile ./Dockerfile, free
   instance, health check /health. No build/start overrides are needed.

2. Environment
   RENDER_EXTERNAL_URL is set by Render and selects the allowed HTTPS origin.
   For a custom domain or another host, set LUCKYPATH_PUBLIC_ORIGIN to its exact
   HTTPS origin, e.g. https://demo.example.com (no path). Only one origin is
   accepted. Changing the host also changes the browser profile-storage origin.
   Optional: add LUCKYPATH_ORS_API_KEY using Render's secret environment field.
   Do not commit this key or paste it into source files. Visitors cannot set keys.
   Never set secrets as Docker build arguments or place them in a .env file in Git.

3. After deploy
   / redirects to /explore; /health reports readiness. Test:
   - Open Profile on a phone and desktop, skip profile and calculate a route.
   - Run the two analyses with synthetic inputs; check their combined priorities.
   - Save and delete browser details, change endpoints/time, and add a stop.
   - Check source download and privacy links under How it works.
   - If ORS is configured, test address search and external comparison separately.
   - Restart/redeploy: app data stays bundled; ORS comes from the environment;
     browser-saved details remain on the same origin. No server profile DB exists.

4. Local production-server check (HTTPS is represented by request headers)
   python -m pip install -r requirements-demo.txt
   python -m pip install --no-deps -e .
   LUCKYPATH_PUBLIC_ORIGIN=https://demo.example.com PORT=8771 \
     gunicorn --config deploy/gunicorn.conf.py 'luckypath.webapp:create_app()'
   GET http://127.0.0.1:8771/health works directly. Other requests must include
   Host: demo.example.com; POST also requires Origin: https://demo.example.com.
   This is a transport check, not a browser preview. For ordinary local use keep
   using the original `python -m luckypath serve --explore` loopback server.

5. Docker check
   docker build -t luckypath-demo .
   docker run --rm -p 8771:10000 \
     -e LUCKYPATH_PUBLIC_ORIGIN=https://demo.example.com luckypath-demo
   The build validates packaged Saju/astrology without runtime downloads.
   Startup also validates the actual map dataset and both analysis providers.

6. Capacity and privacy
   One worker/four threads; at most two calculations in progress. Shared budgets
   per minute: 120 route, 20 profile, 30 external requests. Budgets retain only
   timestamps, not user IPs or input. This is a small demo, not a scaled service.
   Gunicorn access logs are off. Body/coordinate/birth data is not logged by the
   app. Render and upstream services have their own network-metadata policies.
   Provider secrets are runtime environment values; no settings-write endpoint
   is registered in the public app. Public pilot/feedback endpoints do not exist.
   HTTPS is terminated by the host. Request host/origin are checked against the
   configured public URL; X-Forwarded-Host/Proto cannot change that decision.

7. Licensing and source
   THIRD_PARTY_NOTICES.txt and licenses/ preserve the installed dependency notices.
   LuckyPath application code is distributed under AGPL-3.0-only; see LICENSE.
   This does not relicense third-party code or data. Preserve their notices.
   The public app includes /source-download: it serves the allowlisted source
   archive rebuilt from the current source during every Docker build (app source,
   build files, data and notices). A prebuilt copy is included for local testing.
   This archive contains no environment files, keys, profiles, feedback DBs,
   virtualenvs or private workspace documents. It excludes third-party installed
   binaries; pinned dependencies are obtained from their upstream distributions.

8. Portfolio phone embed
   Embed https://luckypath-demo.onrender.com/embed in the portfolio phone frame.
   The document permits only https://jayce-portfolio-phi.vercel.app and the local
   portfolio at http://127.0.0.1:3000 or http://localhost:3000 as frame ancestors.
   All other documents retain frame denial; API origin checks are unchanged.
   The frame sends only {type: 'luckypath:ready'} when its map and controls load.
   Parents must verify both message origin and the iframe contentWindow.
   Open /explore in a separate tab for the full app. Embedded storage availability
   depends on the visitor's browser; profiles are never sent to the portfolio.
   To use another portfolio domain, review and update EMBED_PARENTS in webapp.py.

Official references
   https://render.com/docs/blueprint-spec
   https://render.com/docs/web-services
   https://render.com/docs/free
   https://flask.palletsprojects.com/en/stable/deploying/gunicorn/

Free Render web services sleep after 15 idle minutes and can take about a minute
to wake. Memory/cold-start behavior must be measured on the actual host; a local
test does not establish that the free instance can handle a particular load.
