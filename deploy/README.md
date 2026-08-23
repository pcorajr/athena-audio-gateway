# Deploying the Athena Audio Gateway

Target: **athena** — the Linux workstation at `10.90.10.112` (RTX 4090), which
already runs Hermes. It connects over the LAN to **ATHENA-DCS** (`10.90.10.78`).

## Topology

```text
DCS flight client (Windows)
  DCS + SRS client + HOTAS + headset
  normal cockpit PTT, unchanged
        |  ordinary SRS traffic
        v
ATHENA-DCS  10.90.10.78  (Windows)
  DCS server . SRS server (EAM) . DCS-gRPC . Tacview RT
        |  SRS 5002 . Tacview 42674
        v
athena  10.90.10.112  (Linux, RTX 4090)
  Athena Audio Gateway  +  Hermes
```

Nothing is installed on the flight client. The gateway is an ordinary SRS
client and cannot disrupt the pilot's radios.

## Why athena

| | |
|---|---|
| GPU | RTX 4090, 24 GB — ample for a 466 MB Whisper model |
| CPU | i9-14900KF, 32 cores, AVX2 present |
| SRS reachability | Verified: `10.90.10.78:5002` open from this host |
| Hermes | Already runs here, so the text bridge is a localhost call |
| DCS | Not on this machine, so no contention and no boot conflict |

The last two matter most. Hermes being local removes a network hop and a
firewall rule from the command path, and keeping the gateway off any DCS
machine avoids the configuration upstream explicitly refuses to support
(local CPU Whisper alongside DCS).

**Do not host this on a machine that also runs DCS.** A flight client is
usually booted into Windows and busy rendering; it cannot serve as a Linux
gateway at the same time.

## Before you start

On **ATHENA-DCS**, two things must be true:

1. **SRS External AWACS Mode enabled with a blue password.** `SR-Server.exe`
   shows "ON" next to External AWACS Mode. The server keeps separate blue and
   red passwords (`EXTERNAL_AWACS_MODE_BLUE_PASSWORD` and
   `..._RED_PASSWORD` in `server.cfg`); supply the one matching the
   `coalition` in the gateway config.
2. A mission loaded, if you intend to test.

**Tacview is not required.** Upstream SkyEye needs real-time telemetry to feed
its radar scope, which exists only to answer GCI requests. The Athena lane
never starts the radar — Athena's facts reach Hermes through Athena's own
tools — so the gateway skips telemetry construction entirely in Athena mode.
Nothing needs to be installed or configured on the DCS server for it.

Outbound from athena: `5002/TCP+UDP` (SRS) and `443/TCP` only if using cloud
recognition. **No inbound ports.**

## Install

```sh
# Build dependencies (CachyOS/Arch). go, cmake, opus and libsoxr are already
# present on athena; this is the full list for a fresh host.
sudo pacman -S --needed go cmake base-devel opus libsoxr

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
