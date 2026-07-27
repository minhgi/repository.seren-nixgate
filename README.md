# Seren Nixgate

Personal Kodi repository for a modified Seren build (Simkl + MDBList integration), plus its companion `context.seren` context-menu add-on.

## Install

1. In Kodi: **Settings -> File Manager -> Add source**, and enter this path:

   `https://minhgi.github.io/repository.seren-nixgate/`

   Give it any name (e.g. "Seren Nixgate") and select **OK**.
2. In Kodi: **Add-ons -> Install from zip file** -> select the source you just added -> select `repository.seren-nixgate-1.0.0.zip`.
3. In Kodi: **Install from repository -> Seren Nixgate -> Video add-ons -> Seren** (`context.seren` installs automatically alongside it as a required dependency).
4. **curl_cffi (optional)** — only needed for Cloudflare-impersonation scraping on Kodi 22 / Python 3.14. It's a script module, so it won't appear under Video add-ons; install it directly via **Add-ons -> Install from zip file** -> `repo/zips/script.module.curl_cffi/script.module.curl_cffi-0.15.0.zip`, or paste this URL wherever your Kodi build accepts a zip URL:

   `https://minhgi.github.io/repository.seren-nixgate/repo/zips/script.module.curl_cffi/script.module.curl_cffi-0.15.0.zip`

Once the repository add-on is installed, Kodi checks this repo for updates automatically.
