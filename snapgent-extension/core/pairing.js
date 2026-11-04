// SPDX-License-Identifier: GPL-3.0-or-later
// core/pairing.js - pure helpers for the bridge handshake.
// Kept out of background.js (a service worker, so no modules) to be testable in
// Node: these two functions decide whether a user can connect at all.

const ZSPairing = (() => {
  // The bridge reads ?token= from the handshake URL, so a token copied into the
  // popup must survive the round trip whatever characters it holds.
  function bridgeUrl(baseUrl, token) {
    if (!token) return baseUrl;
    return baseUrl + "/?token=" + encodeURIComponent(token);
  }

  // 1008 = policy violation: the bridge refused the handshake for the token we
  // sent, so the popup has to ask for a new one. Every other close is temporary
  // (1006 network drop, 1012 token rotation) and only needs a reconnect.
  function isAuthClose(event) {
    return !!event && event.code === 1008;
  }

  // 1012 = the bridge rotated its token and closed every authenticated socket on
  // purpose. Not a pairing failure: the new token is already stored, so the popup
  // reports a reconnect instead of showing the pairing card.
  function isRotationClose(event) {
    return !!event && event.code === 1012;
  }

  return { bridgeUrl, isAuthClose, isRotationClose };
})();
