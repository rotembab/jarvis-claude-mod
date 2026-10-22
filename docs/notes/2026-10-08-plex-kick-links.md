# Opening Plex titles and Kick channels on the TVs (research, 2026-10-08)

Question (Rotem, home thread): can Jarvis open Plex / Kick on a specific title or stream?
Method: three researchers (tvOS links, Plex remote control, Android TV links), each re-checked by a skeptic that re-fetched the sources. Nothing was run on a real TV. "Unclear" means no source confirms it on a current build.

## Kick
- Apple TV: works through `https://kick.com/<slug>` (kick.com's apple-app-site-association gives `com.kick.mobile` every path except `/tv_help` and `/go-live`; needs tvOS 26). Already in `plugin/voice/src/jarvis_voice/home/links.py`. Jarvis can only say it "asked".
- Sony: Sony's REST `setActiveApp` takes one `uri` (an app's uri from `getApplicationList`; no link or intent field). Today Jarvis opens Kick and types the name.
- Sony, untested: Android TV Remote Protocol v2 (`androidtvremote2` 0.3.2, Apache-2.0). Pair once with the code shown on the TV, no developer mode; `send_launch_app_command("https://kick.com/<slug>")` sends any string with a scheme as an app link. kick.com's assetlinks.json lists `com.kick.mobile` (handle_all_urls), but nobody confirmed the TV build declares the intent filter. Failure can show up as a chooser or the browser. Test before building.

## Plex on the Apple TV (Plex app 2026.18.0 and later is the "new Plex experience")
- `plex://play/?metadataKey=%2Flibrary%2Fmetadata%2F<ratingKey>&server=<machineIdentifier>` through pyatv `launch_app`: worked for movies on the older app (HA users, a May 2026 blog; shows hit S01E01 and sometimes hang on the splash). Two community reports dated 2 Oct 2026 say it now only opens the home screen. Unclear, leaning no. Needs the server address and an X-Plex-Token to turn a name into a ratingKey (`/hubs/search`; python-plexapi, BSD-3).
- `https://watch.plex.tv/live-tv/...`, `/movie/<slug>`, `/show/<slug>`: watch.plex.tv's association file (Apple CDN copy) claims these paths for `64UQD6MX6T.com.plexapp.plex`. Same mechanism as Kick, but nobody confirmed the tvOS binary honours them. Covers Plex's free catalogue and free live channels, not a private library. No stable slug lookup found. Jarvis already sends any unknown-host link straight to `launch_app`, so this can be tried today with no code change.
- `app.plex.tv` links: no association file (404). Not usable.
- Companion remote control (`/player/playback/playMedia`): Apple TV needs Settings > Remote Control > Advertise as Player; users report the new app no longer advertises. No.
- Infuse deep links (`infuse://`) work with Plex libraries per user reports, if Rotem uses Infuse.

## Plex on the Sony
- Opening the app only: `setActiveApp`, same as Kick.
- Android TV Remote v2 with `https://watch.plex.tv/movie/<slug>`: a Plex staff post (2025-10-25) says the Android app accepts watch.plex.tv links; watch.plex.tv assetlinks lists `com.plexapp.android`. Likely for catalogue titles; whether it plays the user's own copy is unknown. No confirmed Live TV link.
- Own-library item: `plex://server://<machineId>/com.plexapp.plugins.library/library/metadata/<ratingKey>` needed an intent extra over ADB in a 2021 report; the Remote protocol cannot send extras. Unclear. ADB needs developer options and resets on Android 14+.

## Open inputs
Does Rotem run a Plex Media Server, or only use Plex's free catalogue and channels? Which Plex app version is on each TV? Sony Android version (12+ only opens web links in apps verified for that domain).

## Cheapest next test
Say to Jarvis: "open https://watch.plex.tv/live-tv on the Apple TV" and note where Plex lands (live TV guide means universal links work; home screen means they do not).

## Update, same day: tried on a real Apple TV
`plex://preplay/?metadataKey=%2Flibrary%2Fmetadata%2F14779&server=<machineIdentifier>`, sent through the existing `launch_app` of the Apple TV driver (pyatv Companion `_urlS`), opened the show's page in the Plex app. `plex://play/...` has not been tried yet. The Apple TV answers `Open URL failed` for a link no app accepts, and refuses commands while asleep. Kick's `https://kick.com/...` links were refused by tvOS on the same Apple TV, which contradicts the Kick line above. The Plex driver (`plex.py`, `plex_find.py`) builds the preplay link; see the Plex section of `docs/DEVELOPING.md`.
