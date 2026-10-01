// amazon-cognito-identity-js expects Node's `global` (a file, not inline: the page's Content-Security-Policy allows no inline scripts).
window.global = window;
