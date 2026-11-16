// webauthn.ts — the browser half of M74 passkeys (docs/38 "Passkeys").
//
// The backend speaks pywebauthn's JSON serialization: every binary field is an
// UNPADDED base64url STRING. `navigator.credentials` speaks ArrayBuffer. This
// module is the single seam that converts in both directions, so no page ever
// hand-rolls base64 (the classic source of WebAuthn "invalid challenge" bugs:
// padded base64, or `btoa` on a byte > 0x7f).
//
// The two wrappers also translate DOMException into the app's error style:
// a user who dismisses the authenticator prompt (NotAllowedError) is NOT an
// error condition — it resolves to `null` and the caller stays silent — while
// every other failure surfaces as a readable message instead of
// "[object DOMException]".

/** Wire shape of a registration response (`PublicKeyCredentialCreationOptions`). */
export interface PasskeyCreationOptions {
  challenge: string;
  rp: { id?: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: "public-key"; alg: number }[];
  timeout?: number;
  excludeCredentials?: {
    id: string;
    type?: "public-key";
    transports?: AuthenticatorTransport[];
  }[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
}

/** Wire shape of an assertion options (`PublicKeyCredentialRequestOptions`). */
export interface PasskeyRequestOptions {
  challenge: string;
  rpId?: string;
  timeout?: number;
  allowCredentials?: {
    id: string;
    type?: "public-key";
    transports?: AuthenticatorTransport[];
  }[];
  userVerification?: UserVerificationRequirement;
}

/** JSON credential the backend verifies (docs/38 contract). */
export interface PasskeyCredentialJSON {
  id: string;
  rawId: string;
  type: "public-key";
  response: {
    clientDataJSON: string;
    /** Registration only. */
    attestationObject?: string;
    /** Assertion only. */
    authenticatorData?: string;
    signature?: string;
    userHandle?: string | null;
  };
  /** Registration only — echoed back for the `transports` column. */
  transports?: string[];
}

/** base64url (no padding) → bytes. The `=`-stripped form is what both sides send. */
export function base64urlToBuffer(value: string): ArrayBuffer {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

/** bytes → unpadded base64url. Accepts a view so `.buffer` slices stay correct. */
export function bufferToBase64url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    value instanceof ArrayBuffer
      ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Human-readable message for a DOMException the user did not simply cancel. */
function readableMessage(err: DOMException): string {
  switch (err.name) {
    case "InvalidStateError":
      return "This device could not register a passkey — try a different browser or authenticator.";
    case "NotSupportedError":
      return "This authenticator does not support passkeys.";
    case "SecurityError":
      return "The browser blocked the passkey request for this origin.";
    case "ConstraintError":
      return "The authenticator rejected the request — remove the existing screen lock and try again.";
    case "NotAllowedError":
      // Handled by the caller (user cancellation), never reaches here.
      return "The passkey request was cancelled.";
    default:
      return `Passkey request failed: ${err.name}`;
  }
}

/**
 * `navigator.credentials.create` against server-provided options.
 *
 * Returns `null` when the user dismisses the prompt (NotAllowedError) so the
 * caller can stay silent; rethrows every other failure as a readable
 * `Error`. Any non-DOMException failure propagates untouched — swallowing a
 * programming error as a UI message would hide it.
 */
export async function createPasskey(
  options: PasskeyCreationOptions,
): Promise<PasskeyCredentialJSON | null> {
  const creation: PublicKeyCredentialCreationOptions = {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    user: { ...options.user, id: base64urlToBuffer(options.user.id) },
    excludeCredentials: options.excludeCredentials?.map((credential) => ({
      ...credential,
      id: base64urlToBuffer(credential.id),
      type: credential.type ?? "public-key",
    })),
  };

  let created: Credential | null;
  try {
    created = await navigator.credentials.create({ publicKey: creation });
  } catch (err) {
    if (err instanceof DOMException) {
      if (err.name === "NotAllowedError") return null;
      throw new Error(readableMessage(err));
    }
    throw err;
  }
  if (created === null) return null;
  if (!isPublicKeyCredential(created)) {
    throw new Error("The authenticator returned an unexpected credential type.");
  }

  const response = created.response;
  if (!isAttestationResponse(response)) {
    throw new Error("The authenticator returned an unexpected registration response.");
  }

  const credential: PasskeyCredentialJSON = {
    id: created.id,
    rawId: bufferToBase64url(created.rawId),
    type: "public-key",
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      attestationObject: bufferToBase64url(response.attestationObject),
    },
  };
  const transports = response.getTransports?.();
  if (transports !== undefined) {
    credential.transports = transports;
  }
  return credential;
}

/**
 * `navigator.credentials.get` against server-provided options (passwordless
 * login). Same cancellation/error contract as `createPasskey`.
 */
export async function getPasskey(
  options: PasskeyRequestOptions,
): Promise<PasskeyCredentialJSON | null> {
  const request: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    allowCredentials: options.allowCredentials?.map((credential) => ({
      ...credential,
      id: base64urlToBuffer(credential.id),
      type: credential.type ?? "public-key",
    })),
  };

  let asserted: Credential | null;
  try {
    asserted = await navigator.credentials.get({ publicKey: request });
  } catch (err) {
    if (err instanceof DOMException) {
      if (err.name === "NotAllowedError") return null;
      throw new Error(readableMessage(err));
    }
    throw err;
  }
  if (asserted === null) return null;
  if (!isPublicKeyCredential(asserted)) {
    throw new Error("The authenticator returned an unexpected credential type.");
  }

  const response = asserted.response;
  if (!isAssertionResponse(response)) {
    throw new Error("The authenticator returned an unexpected sign-in response.");
  }

  return {
    id: asserted.id,
    rawId: bufferToBase64url(asserted.rawId),
    type: "public-key",
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      authenticatorData: bufferToBase64url(response.authenticatorData),
      signature: bufferToBase64url(response.signature),
      userHandle: response.userHandle === null ? null : bufferToBase64url(response.userHandle),
    },
  };
}

/** Narrow `Credential` to `PublicKeyCredential` without the WebAuthn TS lib. */
function isPublicKeyCredential(credential: Credential): credential is PublicKeyCredential {
  return "rawId" in credential && "response" in credential;
}

function isAttestationResponse(
  response: AuthenticatorResponse,
): response is AuthenticatorAttestationResponse {
  return "attestationObject" in response;
}

function isAssertionResponse(
  response: AuthenticatorResponse,
): response is AuthenticatorAssertionResponse {
  return "authenticatorData" in response && "signature" in response;
}
