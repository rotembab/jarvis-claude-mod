# Home control

Jarvis can control the devices in your home over your home network: an Apple TV, a Sony Bravia TV, Tuya and Smart Life devices (lights, plugs, switches, curtains, fans, heaters), and anything Home Assistant controls. Ask in your own words, by voice or typed:

- "Jarvis, turn on the TV and open Netflix on the Apple TV."
- "Dim the bedroom lights to 30 percent."
- "Pause the Apple TV." "Switch the TV to HDMI 2." "What's the TV on?"
- "Close the living room curtains." "Run movie night."

Claude sees your devices through a tool called `home_control`, finds the one you mean ("the TV", "bedroom light"), and runs the command. Jarvis talks to most devices directly on your network, so they answer quickly and keep working when the internet is down. The exceptions are Smart Life scenes, which run in Tuya's cloud, and the one-time Smart Life link that fetches your Tuya devices' keys.

## Contents

- [Set up your devices](#set-up-your-devices)
  - [Apple TV](#apple-tv)
  - [Sony Bravia TV](#sony-bravia-tv)
  - [Smart Life and Tuya](#smart-life-and-tuya)
  - [Home Assistant](#home-assistant)
- [Using it](#using-it)
- [What Jarvis asks you first](#what-jarvis-asks-you-first)
- [Commands](#commands)
- [Where your devices and keys are kept](#where-your-devices-and-keys-are-kept)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)

## Set up your devices

Run this in Claude Code, or just say "Jarvis, open home setup":

```text
/jarvis home setup
```

A **Jarvis home setup** window opens on your desktop. It has a menu to add each kind of device, rename them, set their rooms, choose which ones Jarvis must ask about first, and try them out. Everything you type there, such as PINs, keys and tokens, goes straight into Jarvis's encrypted store on your PC. None of it passes through Claude Code, your chat or the model. **Never paste a PIN, key or token into the chat.**

The PC and your devices must be on the same home network. Give each TV a fixed address: in your router's settings, reserve its current IP (often called a "DHCP reservation"). Jarvis can find a TV again if its address changes, but that is slower.

**Windows Firewall.** The first time Jarvis searches your network, Windows may ask whether to let Python communicate on networks. Tick **Private networks** only and click **Allow**. If you click Cancel, finding devices automatically stops working. Controlling a device whose address you type in still works.

### Apple TV

1. On the Apple TV, open **Settings > AirPlay and HomeKit > Allow Access** and choose **Anyone on the Same Network** (or **Same Network**).
2. In the setup window, choose **Add an Apple TV**. Pick it from the list, or type its IP address (on the Apple TV: **Settings > Network**).
3. Name it (for example "Apple TV") and give it a room.
4. The TV shows a 4-digit code. Type it into the setup window. After three wrong codes, setup stops; start it again for a new code.

Jarvis can turn the Apple TV on and off, play and pause, skip, navigate, type into a search field, list and open apps, and change the volume. Volume works only if the Apple TV controls your TV's volume over HDMI-CEC; otherwise ask Jarvis to change the TV's volume instead. The Apple TV doesn't tell Jarvis which app is open, so "what's on?" only says whether it is on or asleep. To remove Jarvis later, use **Settings > Remotes and Devices > Remote App and Devices** on the Apple TV.

If no code appears, or the right code is refused, restart the Apple TV (**Settings > System > Restart**) and try again. Some Apple TV 4K units on tvOS 26 have a known pairing bug.

### Sony Bravia TV

1. On the TV, open **Settings > Network & Internet > IP control** (on older Android TVs, **Settings > Network > Home network setup > IP control**):
   - **Authentication**: **Pre-Shared Key** (older TVs: **Normal and Pre-Shared Key**). Never choose **None**: it lets any device on your network control the TV.
   - **Pre-Shared Key**: make up a key, such as 8 random letters and numbers. Don't use a password you use elsewhere, because the key travels unencrypted on your home network.
   - **Control remotely**: **On** (Google TV models).
2. Still under **Settings > Network & Internet**, turn **Remote start** on. Then Jarvis can turn the TV on from standby; it uses a little more power in standby. With it off, the TV disappears from the network when it is off.
3. Optional: if Jarvis still can't turn the TV on from standby, set **Settings > System > Power and energy > Energy modes** to **Increased**. Some models need this.
4. With the TV on, choose **Add a Sony Bravia TV** in the setup window. Pick it from the search, or type its IP address (on the TV: **Settings > Network & Internet**, then your network). Then type the pre-shared key. Jarvis checks the key at once and says if it is wrong or IP control is off.
5. Name it (for example "TV") and give it a room. Jarvis offers a test: it reads the TV's state and presses Home.

Jarvis can turn the TV on and off, set the volume and mute, switch inputs by their names ("Apple TV", "HDMI 2"), open apps, press remote buttons (play, pause, back, home, arrows) and say what is on. With a soundbar, Jarvis turns the sound up and down with the remote's volume buttons, so "volume 20" works only if the TV reports the soundbar's level. The TV can't tell Jarvis whether something is playing, so "play/pause" pauses; say "play" to carry on.

### Smart Life and Tuya

Jarvis controls Tuya devices directly on your network with each device's *local key*. A short, one-time link with your Smart Life (or Tuya Smart) account fetches those keys. You don't need a Tuya developer account.

1. Check that your devices are added and working in the Smart Life (or Tuya Smart) app.
2. In the app, open **Me > Settings (the gear, top right) > Account and Security > User Code**, and note the code.
3. In the setup window, choose **Link Smart Life / Tuya devices**, then **Link Smart Life**, and type the User Code.
4. A QR code appears in the window and as an image. In Smart Life, tap **+** (top right), then **Scan**, scan the code and confirm on the phone within 3 minutes. **The phone may say the login is for Home Assistant.** That is expected: Jarvis signs in the same way Home Assistant's Tuya integration does.
5. Jarvis lists your devices and finds each one on your network. For a switch or plug with several buttons ("gangs"), each becomes its own device; name them when asked. Devices that are off, or run on batteries, may not be found. Jarvis keeps them anyway and looks again when you use them.

Your Smart Life rooms and names carry over. Smart Life **tap-to-run scenes** also show up, and Jarvis can run them ("run movie night"). Scenes are also how Jarvis controls an air conditioner or TV through a Tuya IR blaster: create scenes such as "AC cool 24" and "AC off" in Smart Life, then refresh (below).

Keep the PC and your Tuya devices on the same network, not a guest network or one with Wi-Fi client isolation. The PC can be wired. Jarvis listens for the devices on UDP ports 6666, 6667 and 7000 and reaches them on TCP port 6668, which is why Windows Firewall asks about Python.

The **Link Smart Life / Tuya devices** step also has:

- **Refresh devices and scenes from Smart Life (no QR code)**: after you add a device, re-pair one (its key changes), or add a scene. If Jarvis says the link has expired, choose **Link Smart Life** again.
- **Find my Tuya devices on the network again**: after a device moved or was off during setup.
- **Reverse a curtain's position**: if a curtain moves the wrong way when Jarvis sets its position.

The link uses Home Assistant's Smart Life sign-in. Tuya has not published it for other apps, so it could stop working one day. Your devices keep working locally even then, because Jarvis already has their keys; only scenes and refreshing need the link.

### Home Assistant

If you run Home Assistant, Jarvis can control everything it does, including Hue, Sonos, Shelly, vacuums and Matter devices.

1. In Home Assistant, open your profile (your name, bottom left) > **Security** > **Long-lived access tokens** > **Create token**. Name it "Jarvis" and copy it; Home Assistant shows it only once.
2. In the setup window, choose **Connect Home Assistant**. Type its address (`homeassistant.local` or its IP address is enough), then paste the token. It isn't shown as you paste.
3. If your Home Assistant user is an administrator, Jarvis offers to see only the devices you've exposed to Assist. That is the recommended choice: you pick them in Home Assistant under **Settings > Voice assistants > Expose**.

Names and rooms come from Home Assistant, so change them there. Unlocking, disarming and opening a garage door need your OK on screen; see [What Jarvis asks you first](#what-jarvis-asks-you-first). Jarvis never sends a lock or alarm code: if one needs a code, set a default code for it in Home Assistant, or use the Home Assistant app.

If Home Assistant uses https with a self-signed certificate, setup offers to skip the certificate check. Accept only on your home network.

## Using it

Talk to Jarvis as usual. To see what Jarvis knows, ask "what devices do you have?" or run `/jarvis home list`.

- **Names.** Jarvis understands a device's name, its room ("the bedroom light"), its kind ("the TV", if you have only one), the other names you give it in the setup window, and near misses ("Sonny TV").
- **Two of a kind.** "Turn off the light" with two lights makes Jarvis ask which one.
- **Values.** Volume, brightness and positions go from 0 to 100: "volume 20", "brightness half". Colours are names or hex codes: "warm white", "blue", "#ff8800".
- **Apps and inputs.** "Open YouTube on the TV" matches the app list loosely. So does "switch to the Apple TV" for a TV input you have named "Apple TV".

## What Jarvis asks you first

Most commands just happen, the way a remote control does. A few need your OK on screen first, in a Claude Code dialog: unlocking a lock, disarming an alarm, and opening a garage door or gate. Jarvis never accepts a spoken "yes" for these, because a TV or a video could say it too.

In the setup window, **Ask before Jarvis uses a device** sets any device you added there (not Home Assistant's) to:

- **Just do it**: the default.
- **Ask me on screen first**: for example, a plug that powers a heater.
- **Never**: Jarvis may only read its state.

In plan mode, Jarvis only looks at devices and doesn't change them.

## Commands

| Command | What it does |
| --- | --- |
| `/jarvis home` | What is set up, and where it is saved |
| `/jarvis home setup` | Opens the setup window |
| `/jarvis home list [words]` | Your devices and what each one can do; words filter by name, room or kind |
| `/jarvis home status <device>` | A device's state ("Sony TV is on, volume 20, HDMI 2 (Apple TV)") |

From a terminal on the PC, the same commands work without Claude Code, for example to try a device:

```powershell
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice home list
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice home do "Sony TV" set_volume 20
```

## Where your devices and keys are kept

| File | Holds |
| --- | --- |
| `%USERPROFILE%\.jarvis\home\devices.json` | Your devices: names, rooms, network addresses, what they can do. No secrets. |
| `%USERPROFILE%\.jarvis\home\credentials.dat` | The Apple TV pairing, the Sony pre-shared key, the Tuya local keys and Smart Life link, and the Home Assistant token. They are encrypted with Windows DPAPI, so only your Windows account on this PC can read them. |

To remove a device, use **Rename a device, set its room, or remove it** in the setup window. That also deletes its credentials. To remove everything, delete the `home` folder. Then also remove Jarvis on the devices themselves: unpair it on the Apple TV, change the TV's pre-shared key, and delete the Home Assistant token.

## Privacy

- Jarvis sends device commands from your PC straight to your devices on your network. They don't go through Anthropic, Fish Audio or any cloud.
- Claude sees your device names, rooms, states and commands. It never sees an address, a PIN, a key or a token.
- The Smart Life link signs in to Tuya's cloud once, to fetch your devices and their keys, and again when you refresh. Running a Smart Life scene asks Tuya's cloud to run it.
- Nothing polls your devices in the background. Jarvis talks to a device only when you ask it to do something.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| "No home devices are set up yet" | Run `/jarvis home setup`. |
| A device is "off or unreachable" | Check that it is on and connected to the same network as the PC. For a Sony TV, turn on **Remote start**. Tuya devices must be powered; battery sensors sleep. |
| The Sony TV "refused the pre-shared key" | Re-enter the key: in the setup window, add the TV again with the same address. |
| The Apple TV says pairing failed | See the [Apple TV](#apple-tv) steps: allow access, restart it, and pair again. |
| A Tuya device stopped answering after you re-paired it in Smart Life | Its key changed. Use **Refresh devices and scenes from Smart Life** in the setup window. |
| Tuya setup says the scan could not listen | Close other programs that scan for Tuya devices (another tinytuya tool, for example), then choose **Find my Tuya devices on the network again**. |
| Devices are never found automatically | Windows Firewall may be blocking Python. In **Windows Security > Firewall & network protection > Allow an app through firewall**, allow Python on Private networks, and check that your network is set to Private. You can always type a TV's address instead. |
| Home Assistant "rejected Jarvis's token" | Create a new long-lived token and connect Home Assistant again in the setup window. |
| Home Assistant says the PC is banned | Too many failed logins. Remove the PC's address from `ip_bans.yaml` in Home Assistant's config folder and restart Home Assistant. |
| Claude says home control isn't available | Home control runs on your own PC, so it isn't available in cloud sessions. Run `/jarvis setup` to repair the helper if it isn't installed. |

## How it works

The `home_control` tool belongs to the Jarvis mod. The mod checks each call and sends it to the voice helper on your PC (`jarvis_voice.home`). The helper finds the device, checks the command against the device's tier, and runs it with that device's driver:

- **Apple TV**: [pyatv](https://pyatv.dev), over Apple's Companion protocol.
- **Sony**: the Bravia REST API and IRCC remote codes, with the pre-shared key.
- **Tuya**: [tinytuya](https://github.com/jasonacox/tinytuya) with local keys, fetched once by the Smart Life link ([tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk)).
- **Home Assistant**: its REST and WebSocket APIs.

If the helper isn't running, the mod runs a one-off `jarvis_voice home call` instead. That path never accepts an on-screen confirmation.

Developer notes are in [DEVELOPING.md](DEVELOPING.md#home-control), and the message format is the `home` command in [plugin/protocol/schema.json](../plugin/protocol/schema.json).
