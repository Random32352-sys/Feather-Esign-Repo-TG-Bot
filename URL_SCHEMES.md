# Sideloading App URL Schemes

This document explains how the "Add Source" buttons work for ESign and Feather.

## URL Structure

The buttons use specific custom URL schemes that trigger the respective apps on iOS to open and add the repository source.

### ESign
**Scheme:** `esign://addsource?url=<SOURCE_JSON_URL>`

- **Prefix:** `esign://addsource?url=`
- **Payload:** The direct URL to your `source.json` file.
- **Example:**
  ```
  esign://addsource?url=https://your-repo.github.io/esign/source.json
  ```

### Feather
**Scheme:** `feather://source/<SOURCE_JSON_URL>`

- **Prefix:** `feather://source/`
- **Payload:** The direct URL to your `source.json` file.
- **Note:** Do **not** use `?url=` or encoded URLs for the standard implementation.
- **Example:**
  ```
  feather://source/https://your-repo.github.io/esign/source.json
  ```

## Implementation in `index.html`

In the landing page, these URLs are constructed dynamically using JavaScript:

```javascript
const sourceUrl = 'https://random32352-sys.github.io/Feather-Esign-Repo-TG-Bot/esign/source.json';

// ESign
const esignUrl = 'esign://addsource?url=' + sourceUrl;

// Feather
const featherUrl = 'feather://source/' + sourceUrl;

// Apply to buttons
document.getElementById('esign-btn').href = esignUrl;
document.getElementById('feather-btn').href = featherUrl;
```
