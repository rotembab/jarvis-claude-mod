# Home control

Jarvis can control the devices in your home over your home network: an Apple TV, a Sony Bravia TV, Tuya devices from the Tuya Smart or Smart Life app (lights, plugs, switches, curtains, fans, heaters, Fingerbots), and anything Home Assistant controls. Ask in your own words, by voice or typed:

- "Jarvis, turn on the TV and open Netflix on the Apple TV."
- "Dim the bedroom lights to 30 percent."
- "Pause the Apple TV." "Switch the TV to HDMI 2." "What's the TV on?"
- "Close the living room curtains." "Run movie night."

Claude sees your devices through a tool called `home_control`, finds the one you mean ("the TV", "bedroom light"), and runs the command. Jarvis talks to most devices directly on your network, so they answer quickly and keep working when the internet is down. The exceptions are Tuya scenes, which run in Tuya's cloud, the one-time Tuya account link that fetches your Tuya devices' keys, and [Tuya's cloud fallback](#tuyas-cloud-fallback), only if you turn it on.

## Contents

- [Set up your devices](#set-up-your-devices)
  - [Apple TV](#apple-tv)
  - [Sony Bravia TV](#sony-bravia-tv)
  - [Tuya Smart and Smart Life](#tuya-smart-and-smart-life)
    - [Fingerbots and other button pushers](#fingerbots-and-other-button-pushers)
    - [Tuya's cloud fallback](#tuyas-cloud-fallback)
  - [Home Assistant](#home-assistant)
- [Find what's on your network](#find-whats-on-your-network)
- [Siri and Apple Home](#siri-and-apple-home)
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

A **Jarvis home setup** window opens on your desktop. It has a menu to add each kind of device, find smart devices on your network, rename them, set their rooms, choose which ones Jarvis must ask about first, and try them out. Everything you type there, such as PINs, keys and tokens, goes straight into Jarvis's encrypted store on your PC. None of it passes through Claude Code, your chat or the model. **Never paste a PIN, key or token into the chat.**

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

1. On the TV, open **Settings > Network & Internet > IP control** (on some models it is inside **Local network setup** or **Home network setup**; on older Android TVs, **Settings > Network > Home network setup > IP control**):
   - **Authentication**: **Pre-Shared Key** (or **Normal and Pre-Shared Key**). Never choose **None**: it lets any device on your network control the TV. TVs made since August 2025 have no Authentication item; just set the key.
   - **Pre-Shared Key**: make up a key of 16 to 20 random letters and numbers (TVs made since August 2025 need at least 16). Don't use a password you use elsewhere: on older TVs the key travels unencrypted on your home network.
2. On Google TV models, set **Settings > Network & Internet > Remote device settings > Control remotely** to **On**. On some older Google TVs, **Control remotely** is inside IP control instead.
3. Still under **Settings > Network & Internet**, turn **Remote start** on (some models say **On (Powered on by apps)** or **On (Networked standby)**). Then Jarvis can turn the TV on from standby; it uses a little more power in standby. With it off, the TV disappears from the network when it is off.
4. If the TV has had the Android 14 update, also set **Settings > System > Power and Energy > Energy Mode** to **Increased**. **Low** turns the network off in standby, so Jarvis can't turn the TV on, and the update sets Low if Remote start was off before.
5. With the TV on, choose **Add a Sony Bravia TV** in the setup window. Pick it from the search, or type its IP address (on the TV: **Settings > Network & Internet**, then your network); newer TVs may not answer the search. Then type the pre-shared key. Jarvis checks the key at once and says if it is wrong or IP control is off.
6. Name it (for example "TV") and give it a room. Jarvis offers a test: it reads the TV's state and presses Home.

TVs made since August 2025 accept only a secure (HTTPS) connection. Setup tries both kinds and keeps the one that works. If a TV Jarvis already knows starts to need a secure connection, Jarvis switches to it by itself when it can; otherwise it asks you to add the TV again.

Jarvis can turn the TV on and off, set the volume and mute, switch inputs by their names ("Apple TV", "HDMI 2"), open apps, press remote buttons (play, pause, back, home, arrows) and say what is on. With a soundbar or receiver on HDMI (eARC), Jarvis always uses the remote's volume and mute buttons, because the TV's own volume setting can silently do nothing then. So "volume 20" works only if the TV reports the soundbar's level, and the TV's status leaves the volume out. The TV can't tell Jarvis whether something is playing, so "play/pause" pauses; say "play" to carry on.

Some budget models (BRAVIA 2 II and some BRAVIA 3) list only **Simple IP control** and **Control4** under IP control. Jarvis can't control those.

### Tuya Smart and Smart Life

Jarvis controls Tuya devices directly on your network with each device's *local key*. A short, one-time link with your Tuya account fetches those keys. It works with the Tuya Smart app and with the Smart Life app. You don't need a Tuya developer account.

Use the same app, the one your devices are in, for every step. Tuya Smart and Smart Life have separate accounts, so a User Code from one app doesn't work with a scan from the other.

1. Check that your devices are added and working in the app.
2. In the app, open **Me**, tap the gear (top right), then **Account and Security**. Your **User Code** is at the bottom. Note it exactly: it is case-sensitive.
3. In the setup window, choose **Link Tuya devices (Tuya Smart or Smart Life)**, then **Link your Tuya account** if it asks, and type the User Code.
4. A QR code appears in the window and as an image. In the same app, go to the **Home** tab and tap **+** (top right), then **Scan**. Scan the code, then tap **Confirm login** on the phone. Use the app's scanner, not the phone's camera, and do it straight away: the code is valid only briefly. **The phone may say the login is for Home Assistant.** That is expected: Jarvis signs in the same way Home Assistant's Tuya integration does. If the code expires or the phone refuses the login, Jarvis says so and offers a new code.
5. Jarvis asks whether to also use Tuya's cloud when a device doesn't answer on your home network. Pressing Enter means no; see [Tuya's cloud fallback](#tuyas-cloud-fallback).
6. Jarvis lists your devices and finds each one on your network. For a switch or plug with several buttons ("gangs"), each becomes its own device; name them when asked. For a Fingerbot, it asks what the Fingerbot presses ([below](#fingerbots-and-other-button-pushers)). Devices that are off, or run on batteries, may not be found. Jarvis keeps them anyway and looks again when you use them.

Your rooms and names carry over from the app. **Tap-to-Run scenes** also show up, and Jarvis can run them ("run movie night"), including the ones you added to Siri. Scenes are also how Jarvis controls an air conditioner or TV through a Tuya IR blaster: create scenes such as "AC cool 24" and "AC off" in the app, then refresh (below).

Keep the PC and your Tuya devices on the same network, not a guest network or one with Wi-Fi client isolation. The PC can be wired. Jarvis listens for the devices on UDP ports 6666, 6667 and 7000 and reaches them on TCP port 6668, which is why Windows Firewall asks about Python.

The **Link Tuya devices** step also has:

- **Refresh devices and scenes from your Tuya account (no QR code)**: after you add a device, re-pair one (its key changes), or add a scene. If Jarvis says the link has expired, choose **Link your Tuya account** again.
- **Use Tuya's cloud when a device does not answer on the network**: turns the [cloud fallback](#tuyas-cloud-fallback) on or off. The item says whether it is on now.
- **Find my Tuya devices on the network again**: after a device moved or was off during setup.
- **Reverse a curtain's position**: if a curtain moves the wrong way when Jarvis sets its position.

The link uses Home Assistant's Tuya sign-in. Tuya has not published it for other apps, so it could stop working one day. Your devices keep working locally even then, because Jarvis already has their keys; only scenes, refreshing and the cloud fallback need the link.

#### Fingerbots and other button pushers

A Fingerbot, or another Tuya button pusher, sits on a wall switch and presses it for you. Jarvis can press it: "press the Fingerbot", or "turn on the bedroom light" once setup knows what it presses. Jarvis can't see the light, so it never says whether the light is on or off; it says it pressed the switch. It presses for "turn on" and "turn off" alike, so turning off a light that is already off turns it on.

- **It needs a Tuya gateway.** A Fingerbot talks Bluetooth, to your phone. Jarvis can reach it only through a Tuya Bluetooth or multi-mode gateway on your network. Until there is one, setup leaves the Fingerbot out and says why. Add a gateway in the Tuya Smart or Smart Life app, pair the Fingerbot to it, then choose **Refresh devices and scenes** in the setup window.
- **Name what it presses.** When setup first adds a Fingerbot, it asks what it switches on and off, for example "bedroom light". Jarvis keeps that as another name for it, so "turn on the bedroom light" finds the Fingerbot. A refresh keeps the name and doesn't ask again. To change it, use **Rename a device, set its room, or remove it > Set other names (aliases)**.
- **Use Click mode.** Set the Fingerbot to Click mode in the app, so each press is one push on the switch. In Switch mode, a press moves its arm down or up instead. Jarvis doesn't change the mode.

Ask Jarvis about it and it says what the Fingerbot presses, its mode and its battery. Jarvis also knows it as "the button", "the clicker" or "the button pusher".

#### Tuya's cloud fallback

Jarvis controls your Tuya devices over your home network. With the cloud fallback on, it can also send a command through Tuya's cloud, over the internet, when a device doesn't answer at home. It is off unless you turn it on.

- **Turning it on or off.** Jarvis asks right after you link your Tuya account. To change your answer, choose **Link Tuya devices (Tuya Smart or Smart Life)** in the setup window, then **Use Tuya's cloud when a device does not answer on the network**. After turning it on, choose **Refresh devices and scenes** to add devices only the cloud can reach.
- **What it does.** Jarvis always tries your home network first. Only if the device doesn't answer, takes too long or turns Jarvis away does the same command go through Tuya's cloud, and Jarvis's answer then ends "through Tuya's cloud". A device Jarvis hasn't found on the network goes to the cloud at once, while Jarvis looks for it in the background so the next command can stay at home. A value the device refused, or a command it doesn't have, never goes to the cloud. A toggle or a Fingerbot press is never sent twice.
- **Devices only the cloud reaches.** With the fallback on, a refresh also adds devices Tuya gives no local key for and devices behind a hub Jarvis couldn't match. Setup lists them as "through Tuya's cloud only". If you turn the fallback off, Jarvis can't control them until you turn it on again.
- **What it can't do.** A Bluetooth device with no Tuya gateway, such as a Fingerbot paired only to your phone, can't be reached either way. If Tuya's cloud says a device is offline, Jarvis says so. If Tuya limits requests, Jarvis makes no cloud calls for a minute, scenes included.
- **Devices saved earlier.** Colours and some brightness ranges on devices Jarvis saved before the fallback existed need **Refresh devices and scenes** before the cloud can set them. On and off work right away.

The fallback uses the same unofficial sign-in as the link. If Tuya blocks it, the fallback and scenes stop working, and control over your home network carries on.

### Home Assistant

If you run Home Assistant, Jarvis can control everything it does, including Hue, Sonos, Shelly, vacuums and Matter devices.

1. In Home Assistant, open your profile (your name, bottom left) > **Security** > **Long-lived access tokens** > **Create token**. Name it "Jarvis" and copy it; Home Assistant shows it only once.
2. In the setup window, choose **Connect Home Assistant**. Type its address (`homeassistant.local` or its IP address is enough), then paste the token. It isn't shown as you paste.
3. If your Home Assistant user is an administrator, Jarvis offers to see only the devices you've exposed to Assist. That is the recommended choice: you pick them in Home Assistant under **Settings > Voice assistants > Expose**.

Names and rooms come from Home Assistant, so change them there. Unlocking, disarming and opening a garage door need your OK on screen; see [What Jarvis asks you first](#what-jarvis-asks-you-first). Jarvis never sends a lock or alarm code: if one needs a code, set a default code for it in Home Assistant, or use the Home Assistant app.

If Home Assistant uses https with a self-signed certificate, setup offers to skip the certificate check. Accept only on your home network.

## Find what's on your network

Jarvis can search your home network and tell you which smart devices are there and which of them it can control. Use any of these:

- In the setup window, choose **Find smart devices on my network**. Do your first search here: if Windows Firewall asks about Python, the question comes up where you can answer it.
- Type `/jarvis home scan` in Claude Code.
- Ask Jarvis, for example "what smart devices are on my network?"

A search takes about ten seconds. It only asks the network who is there, and reads the description a device offers about itself. It doesn't pair with anything, sign in or change anything, and it saves nothing. Jarvis searches at most once a minute; ask again sooner and you get the last answer.

The answer puts what it found in groups:

| Group | What it means |
| --- | --- |
| Jarvis controls these | Apple TVs, Sony Bravia TVs, Tuya devices and Home Assistant. Each says the name Jarvis knows it by, or that it isn't set up yet and how to add it in home setup. |
| Jarvis could control these with a new driver | Brands Jarvis doesn't support yet, such as Philips Hue or Sonos, and what adding one would take. |
| In Apple Home | HomeKit devices already in Apple's Home app. Siri controls them, and Jarvis can't share them; see [Siri and Apple Home](#siri-and-apple-home). |
| HomeKit devices not in any home yet | HomeKit devices nothing has paired with yet. Jarvis doesn't control HomeKit devices. |
| Matter devices | Jarvis can't control Matter devices yet; the app that set them up can. Ones already set up can only be counted, per Matter home. |

Printers, computers, phones and routers are only counted. Bluetooth and Zigbee devices never show up in a network search, only their hub or gateway does, so a Fingerbot paired only to your phone won't appear.

Claude sees the devices' names and kinds, never their addresses or ids.

If nothing answers, Windows Firewall may be blocking Python, or your network may be set to Public. In **Windows Security > Firewall & network protection > Allow an app through firewall**, allow Python on Private networks, and check that your network is set to Private. Govee and Yeelight lights answer only when **LAN Control** is on in their own app.

## Siri and Apple Home

Jarvis runs on Windows, and Apple gives Windows programs no way into Apple Home. So Jarvis can't run your Apple Home scenes or control accessories that live only in Apple's Home app. An accessory already in Apple Home also won't accept a second controller unless you remove it there, which would take it away from Siri.

- **The Apple TV.** Pairing it in Jarvis makes Jarvis a remote for the Apple TV, not a HomeKit controller. It doesn't give Jarvis your Home accessories.
- **Tuya scenes in Siri.** A Tap-to-Run scene you added to Siri from the Tuya Smart or Smart Life app is the same scene Jarvis runs once your Tuya account is linked.
- **Devices Jarvis reaches itself.** A Sony TV or Tuya devices work with Jarvis whether or not they are also in Apple Home.

A network search shows which devices it sees in Apple Home. A Mac version of Jarvis could one day run your Shortcuts, and Apple Home through them; there is none yet.

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

Your Claude Code permission rules apply too. Jarvis's device tool is `mcp__jarvis__home_control`, and you can add rules for it with `/permissions`:

- **Deny rule**: Jarvis can't change any device. `/jarvis home` still works when you type it.
- **Ask rule**: Jarvis asks on screen before every change, whatever the device's setting.
- **dontAsk mode**: changes are refused unless you have an allow rule for the tool.

Listing devices, reading their state and searching the network are always allowed.

These checks cover Jarvis's own device tool. Programs that run under your Windows account can still use the helper, and that includes shell commands Claude runs: DPAPI keeps your keys from other accounts, not from your own programs. Claude Code asks before it runs a shell command you haven't allowed, and that prompt is what stops Claude going around the tool. So don't allow commands that run Jarvis's Python (`.jarvis\venv`) without asking.

## Commands

| Command | What it does |
| --- | --- |
| `/jarvis home` | What is set up, and where it is saved |
| `/jarvis home setup` | Opens the setup window |
| `/jarvis home list [words]` | Your devices and what each one can do; words filter by name, room or kind |
| `/jarvis home status <device>` | A device's state ("The Sony TV is on, showing Apple TV (HDMI 2), volume 20%.") |
| `/jarvis home do <device> -- <command> [value]` | Runs one command, for example `/jarvis home do Sony TV -- set_volume 20`. Asks on screen first where the device needs it. |
| `/jarvis home scan` | Searches your home network for smart devices, about ten seconds; see [Find what's on your network](#find-whats-on-your-network) |

From a terminal on the PC, the same commands work without Claude Code, for example to try a device. There, a command that needs confirming asks you to type yes:

```powershell
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice home list
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice home do "Sony TV" set_volume 20
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice home scan
```

## Where your devices and keys are kept

| File | Holds |
| --- | --- |
| `%USERPROFILE%\.jarvis\home\devices.json` | Your devices: names, rooms, network addresses, what they can do. No secrets. |
| `%USERPROFILE%\.jarvis\home\credentials.dat` | The Apple TV pairing, the Sony pre-shared key, the Tuya local keys and your Tuya account link (with your cloud fallback choice), and the Home Assistant token. They are encrypted with Windows DPAPI, so only your Windows account on this PC can read them. |

To remove a device, use **Rename a device, set its room, or remove it** in the setup window. That also deletes its credentials. To remove everything, delete the `home` folder. Then also remove Jarvis on the devices themselves: unpair it on the Apple TV, change the TV's pre-shared key, and delete the Home Assistant token.

## Privacy

- Jarvis sends device commands from your PC straight to your devices on your network. They don't go through Anthropic, Fish Audio or any cloud, with one exception that you choose: with [Tuya's cloud fallback](#tuyas-cloud-fallback) on, a Tuya command goes through Tuya's cloud when the device doesn't answer at home, or when only the cloud can reach it.
- Claude sees your device names, rooms, states and commands, and the names and kinds of devices a network search finds. It never sees an address, a PIN, a key or a token.
- The Tuya link signs in to Tuya's cloud once, to fetch your devices and their keys, and again when you refresh. Running a Tuya scene asks Tuya's cloud to run it, and so does a command that falls back to the cloud.
- The helper's log never holds a Tuya key, token or reply from Tuya's cloud. For a Tuya command it records the device's Tuya id, whether it went over your network or the cloud, and error codes.
- Nothing polls your devices in the background. Jarvis talks to a device, or searches the network, only when you ask it to.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| "No home devices are set up yet" | Run `/jarvis home setup`. |
| A device is "off or unreachable" | Check that it is on and connected to the same network as the PC. For a Sony TV, turn on **Remote start**, and after the Android 14 update set **Energy Mode** to **Increased**. Tuya devices must be powered; battery sensors sleep. |
| The Sony TV "refused the pre-shared key", or "now needs a secure connection" | Re-enter the key: in the setup window, add the TV again with the same address. Setup finds by itself whether the TV needs a secure connection. |
| Sony setup says the TV "refused the connection" | Check that IP control and **Control remotely** are on (see the [Sony Bravia TV](#sony-bravia-tv) steps). If IP control lists only **Simple IP control** and **Control4**, Jarvis can't control this model. |
| The Apple TV says pairing failed | See the [Apple TV](#apple-tv) steps: allow access, restart it, and pair again. |
| The phone says "Please use the designated APP to scan the code to log in" | Scan with the same app you took the User Code from: Tuya Smart and Smart Life have separate accounts. Only those two apps work; if your devices are in another brand's app, move them to Tuya Smart or Smart Life. Jarvis offers a new QR code to scan. If you already used the same app, Tuya may be having trouble on its side: try again later. |
| The QR code expired | Say yes when Jarvis offers a new code, and scan it straight away: a code is valid only briefly. If the wait ran out, choose **Link your Tuya account** again. |
| "The Tuya link has expired", or Tuya's cloud keeps turning Jarvis's sign-in away | In the setup window, choose **Link Tuya devices (Tuya Smart or Smart Life)**, then **Link your Tuya account**, and scan a new code. |
| A Tuya device stopped answering after you re-paired it in the app | Its key changed. Use **Refresh devices and scenes from your Tuya account** in the setup window. |
| A Fingerbot is "Not added" because it is a Bluetooth device | It needs a Tuya Bluetooth or multi-mode gateway. Add one in the Tuya Smart or Smart Life app, pair the Fingerbot to it, then choose **Refresh devices and scenes**. See [Fingerbots](#fingerbots-and-other-button-pushers). |
| Tuya setup or the network search says it could not listen for Tuya devices | Another program has the ports. Close other programs that scan for Tuya devices (another tinytuya tool, for example), wait a few seconds for any Tuya search Jarvis is running to finish, then try again (in setup, **Find my Tuya devices on the network again**). |
| Devices are never found automatically, or a network search finds nothing | Windows Firewall may be blocking Python. In **Windows Security > Firewall & network protection > Allow an app through firewall**, allow Python on Private networks, and check that your network is set to Private. You can always type a TV's address instead. |
| Home Assistant "rejected Jarvis's token" | Create a new long-lived token and connect Home Assistant again in the setup window. |
| Home Assistant says the PC is banned | Too many failed logins. Remove the PC's address from `ip_bans.yaml` in Home Assistant's config folder and restart Home Assistant. |
| Claude says home control isn't available | Home control runs on your own PC, so it isn't available in cloud sessions. Run `/jarvis setup` to repair the helper if it isn't installed. |

## How it works

The `home_control` tool belongs to the Jarvis mod. The mod checks each call and sends it to the voice helper on your PC (`jarvis_voice.home`). The helper finds the device, checks the command against the device's tier, and runs it with that device's driver:

- **Apple TV**: [pyatv](https://pyatv.dev), over Apple's Companion protocol.
- **Sony**: the Bravia REST API and IRCC remote codes, over HTTP or HTTPS, with the pre-shared key.
- **Tuya**: [tinytuya](https://github.com/jasonacox/tinytuya) with local keys, fetched once by the Tuya account link ([tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk)). The cloud fallback sends commands through the same SDK.
- **Home Assistant**: its REST and WebSocket APIs.

A network search (`scan`) needs no driver: it uses [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) for mDNS, an SSDP search, tinytuya's scanner without connecting to anything, and the discovery messages of Kasa, LIFX, WiZ, Yeelight and Govee.

If the helper isn't running, the mod runs a one-off `jarvis_voice home call` instead. That path never accepts an on-screen confirmation.

Developer notes are in [DEVELOPING.md](DEVELOPING.md#home-control), and the message format is the `home` command in [plugin/protocol/schema.json](../plugin/protocol/schema.json).
