# Third-Party Font Licenses

SiGMA bundles the following fonts for its in-app code editor. All are
licensed under the SIL Open Font License 1.1 (OFL-1.1), which permits free
use, redistribution, and bundling in open-source software, provided the
license is preserved.

The fonts are pulled in as npm packages from [Fontsource](https://fontsource.org/)
(see `frontend/package.json`) and statically imported in
`frontend/src/main.jsx`.

## Fontsource packages

| Font            | npm package                          | License  |
|-----------------|--------------------------------------|----------|
| JetBrains Mono  | `@fontsource/jetbrains-mono`         | OFL-1.1  |
| Fira Code       | `@fontsource/fira-code`              | OFL-1.1  |
| Cascadia Code   | `@fontsource/cascadia-code`          | OFL-1.1  |
| Source Code Pro | `@fontsource/source-code-pro`        | OFL-1.1  |
| Roboto Mono     | `@fontsource/roboto-mono`            | OFL-1.1  |

## Font sources and authors

- **JetBrains Mono** — by JetBrains. <https://www.jetbrains.com/lp/mono/> ·
  <https://github.com/JetBrains/JetBrainsMono>
- **Fira Code** — by Nikita Prokopov. <https://github.com/tonsky/FiraCode>
- **Cascadia Code** — by Microsoft. <https://github.com/microsoft/cascadia-code>
- **Source Code Pro** — by Adobe. <https://github.com/adobe-fonts/source-code-pro>
- **Roboto Mono** — by Google. <https://github.com/googlefonts/roboto-mono>

## SIL Open Font License 1.1

The full text of the OFL-1.1 is available at:
<https://openfontlicense.org/open-font-license-official-text/>

A redistribution obligation under the OFL is to "include the copyright notice,
the license, and any trademarks with the Font Software". The copyright notices
and trademark statements for each font ship inside the corresponding
`@fontsource/*` package under `node_modules/@fontsource/*/LICENSE` and are
preserved in the dependency tree. The OFL-1.1 full text is also available in
those files.

If you redistribute a built bundle of SiGMA, ensure these LICENSE files
travel with the bundled fonts (the Vite build pulls the font binaries from the
imported `.css`/`.woff2` assets, which name the font family; keep this file in
the repository as the human-readable attribution).
