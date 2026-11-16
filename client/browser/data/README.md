# Browser data bundled with releases

Release builds place the checksum-verified GeoIP database and signed uBlock Origin
XPI pinned by `../assets.lock.json` in this directory. Do not commit these binary
assets to the source repository.

GeoIP matching is attempted by default. Normal installation tries to prepare the
verified database along with browser assets. Keep a verified versioned copy in
deployment packages or the download cache when upstream releases are retired.
Missing, invalid or unavailable GeoIP produces a warning and allows acquisition
to continue without matching. Runtime startup never downloads a database. See
[installation and operation](../../../docs/running.md).

## uBlock Origin

Upstream: [gorhill/uBlock](https://github.com/gorhill/uBlock).
The original Mozilla XPI is bundled without modification, retaining its signature
and included `LICENSE.txt` and third-party notices. Installation verifies the
archive hash and manifest version before loading the extension.

## GeoIP

Upstream: [daijro/geoip-all-in-one](https://github.com/daijro/geoip-all-in-one).
Its [source list and attributions](https://github.com/daijro/geoip-all-in-one#licenses)
include:

- IP2Location LITE: https://lite.ip2location.com/
- GeoLite2: this product includes GeoLite2 Data created by MaxMind,
  available from https://www.maxmind.com/.
- DB-IP Lite: https://db-ip.com/
- Further country data sources are listed in the upstream README.
