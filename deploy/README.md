# Deploying the Athena Audio Gateway

Target: **darkstar-79** (Linux, RTX 5090), connecting over the LAN to
**ATHENA-DCS** (`10.90.10.78`).

Nothing is installed on the playable DCS/VR client. The gateway is an ordinary
SRS client; it cannot disrupt the pilot's radios.

## Why here and not on the DCS server

| Placement | Speech recognition | Verdict |
|---|---|---|
| **darkstar-79** | Local Whisper on GPU | **Chosen.** Idle GPU, no DCS load, audio never leaves the LAN. |
| ATHENA-DCS | Cloud API | Workable fallback. Sends audio to a third party. |
| ATHENA-DCS | Local Whisper on CPU | **Unsupported upstream.** SkyEye's author explicitly refuses to support this alongside DCS. |

The trade-off: the GPU build is flagged experimental upstream. If it proves
unstable, switch `recognizer` to `openai-whisper-api`.

## Before you start

On **ATHENA-DCS**, three things must already be true:

1. **SRS External AWACS Mode enabled with a password.** `SR-Server.exe` shows
   "ON" next to External AWACS Mode.
2. **Tacview Real-Time Telemetry enabled** — DCS → OPTIONS → SPECIAL → Tacview.
   This is a SkyEye startup dependency, not an Athena one; Athena's facts come
   through Hermes tools, not SkyEye's radar. The process will not start without
   it.
3. A mission loaded, if you intend to test.

Outbound from darkstar-79: `5002/TCP+UDP` (SRS), `42674/TCP` (Tacview),
`443/TCP` only if using cloud recognition. **No inbound ports.**

## Install

```sh
# GPU driver and Vulkan loader (CachyOS/Arch)
sudo pacman -S --needed nvidia vulkan-icd-loader opus libsoxr
vulkaninfo | head -5   # confirm the GPU is visible

# Build. Upstream forbids bare `go build` — CGO flags and libwhisper.a are
# required. Use make.
git clone git@github.com:pcorajr/athena-audio-gateway.git
cd athena-audio-gateway
git checkout athena/main
make whisper
make skyeye

# Service account with no login and no home directory
sudo useradd --system --no-create-home --shell /usr/sbin/nologin athena-gateway
sudo usermod -aG video,render athena-gateway

# Layout
sudo mkdir -p /opt/athena-gateway/{bin,models} /etc/athena-gateway
sudo cp skyeye /opt/athena-gateway/bin/skyeye
sudo curl -L -o /opt/athena-gateway/models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
sudo chown -R athena-gateway:athena-gateway /opt/athena-gateway

# Config — contains the SRS password, so lock it down
sudo cp deploy/athena-gateway.yaml /etc/athena-gateway/config.yaml
sudo chown -R athena-gateway:athena-gateway /etc/athena-gateway
sudo chmod 600 /etc/athena-gateway/config.yaml
sudo -e /etc/athena-gateway/config.yaml   # set srs-eam-password and check the frequency

# Service
sudo cp deploy/athena-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
```

**Do not enable the service yet.** Validate by hand first.

## Validate before enabling

### 1. Config is rejected when wrong

These must fail immediately, before any network connection:

```sh
# Typo'd modulation
/opt/athena-gateway/bin/skyeye --athena-command-frequency=30.0XX
# -> FTL --athena-command-frequency must end in AM or FM

# Frequency without a pilot
/opt/athena-gateway/bin/skyeye --athena-command-frequency=30.0FM
# -> FTL invalid admission config: pilot name must be set

# Hermes endpoint without the gate
/opt/athena-gateway/bin/skyeye --athena-hermes-endpoint=http://127.0.0.1:8080/x
# -> FTL athena-hermes-endpoint requires the admission gate
```

If any of these *starts* instead of failing, stop — the safety ordering is
broken and the gateway must not be run.

### 2. Confirm mute is real

```sh
/opt/athena-gateway/bin/skyeye --version   # must be v1.10.0-10-... or later
```

Earlier builds carry the upstream bug where `--mute` was accepted and ignored.
Confirm `mute: true` is set in the config.

### 3. Receive-only run

With `athena-hermes-endpoint` still commented out and `mute: true`:

```sh
sudo -u athena-gateway /opt/athena-gateway/bin/skyeye \
  --config-file=/etc/athena-gateway/config.yaml
```

Expect, in order:

```
INF parsed Athena command channel frequencyHz=30000000 modulation=FM
INF Athena command-channel admission gate enabled ... pilot=Prometheus
INF syncing with SRS server
INF reconnecting to external AWACS mode
```

`Athena` should now appear in the SRS client list.

Have Prometheus tune 30.0 FM and speak. Expect a transcription attempt in the
journal. Then have someone else transmit on the same frequency — expect:

```
INF transmission rejected before speech recognition reason=speaker_not_authorized
```

That single line is the whole security model working: rejection happens
*before* speech recognition, so nothing is transcribed for an unauthorized
speaker.

### 4. Enable

Only once the above behaves:

```sh
sudo systemctl enable --now athena-gateway
journalctl -fu athena-gateway
```

## Turning on Hermes

Uncomment `athena-hermes-endpoint`. The GCI lane is then never started — no
brevity parser, no radar scope, no GCI controller. Keep `mute: true` until
issue #181's transmit gate is satisfied; the gateway will exchange text with
Hermes and simply not speak the reply.

## Operating notes

```sh
systemctl status athena-gateway
journalctl -fu athena-gateway
journalctl -u athena-gateway | grep 'rejected before speech recognition'
```

**Silence is normal.** Every uncertainty path is deliberately quiet: empty
transcript, bridge error, Hermes suppression. If the gateway says nothing, read
the journal before assuming it is broken.

**Before any live transmit test**, audit upstream — see the tracking policy in
`ATHENA.md`. A pinned tag once carried a bug that made `--mute` inert.

## Uninstall

```sh
sudo systemctl disable --now athena-gateway
sudo rm /etc/systemd/system/athena-gateway.service
sudo systemctl daemon-reload
sudo rm -rf /opt/athena-gateway /etc/athena-gateway
sudo userdel athena-gateway
```

Normal DCS and SRS operation is unaffected: the gateway was only ever a client.
