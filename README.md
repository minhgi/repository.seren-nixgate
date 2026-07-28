# Seren Nixgate

Personal Kodi repository for a modified Seren build (Simkl + MDBList integration), plus its companion `context.seren` context-menu add-on.

## Install

1. In Kodi: **Settings -> File Manager -> Add source**, and enter this path:

   `https://minhgi.github.io/repository.seren-nixgate/`

   Give it any name (e.g. "Seren Nixgate") and select **OK**.
2. In Kodi: **Add-ons -> Install from zip file** -> select the source you just added -> select `repository.seren-nixgate-1.0.0.zip`.
3. In Kodi: **Install from repository -> Seren Nixgate -> Video add-ons -> Seren** (`context.seren` installs automatically alongside it as a required dependency).

   The original (non-fork) Seren 3.0.62 is not needed before installing via this repository. If step 3 fails or behaves oddly, install Seren 3.0.62 from Kodi's official Add-on repository first, then retry step 3.
4. **curl_cffi (optional)** — only needed for Cloudflare-impersonation scraping on Kodi 22 / Python 3.14. It's a script module, so it won't appear under Video add-ons; install it directly via **Add-ons -> Install from zip file** -> `repo/zips/script.module.curl_cffi/script.module.curl_cffi-0.15.0.zip`.

   If that link doesn't resolve inside Kodi's file-manager browse, use **Install from zip file -> Web Location...** with this exact address instead:

   `https://minhgi.github.io/repository.seren-nixgate/repo/zips/script.module.curl_cffi/script.module.curl_cffi-0.15.0.zip`

Once the repository add-on is installed, Kodi checks this repo for updates automatically.
