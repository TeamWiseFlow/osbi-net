# Third-Party Notices

OSBI-Net's own code is covered by the root [LICENSE](LICENSE). Dependencies and
downloaded browser assets retain their respective licenses and notices.

- Apache ECharts 5.5.1: Apache-2.0. The distributed dashboard script retains its
  upstream header; its [license](client/backend/osbi_client/static/echarts.LICENSE.txt)
  and [notice](client/backend/osbi_client/static/echarts.NOTICE.txt) accompany it.
- Camoufox JavaScript SDK 0.5.9: MIT.
- Playwright Core 1.62.1: Apache-2.0.
- fpgen-js 1.3.0: Apache-2.0.
- maxmind 5.0.0, pdf-lib 1.17.1, TypeBox 1.3.34, adm-zip 0.6.1: MIT.
- Python dependencies are installed separately with their wheel/sdist license
  metadata; exact tested versions are in [requirements.lock](requirements.lock).
- Browser, uBlock Origin and GeoIP data are installed separately from pinned
  upstream distributions, retaining their included notices. See the
  [browser data attribution](client/browser/data/README.md).

The browser runtime's dependency tree and paired asset pins are maintained in
its own package/lock files. Do not remove upstream notices when packaging it.
