// Sign-in against the Cognito user pool (SRP: the password never leaves the
// browser in clear). Invited users first sign in with a temporary password
// and must choose a new one. The ID token (which carries custom:tenant) is
// what the API accepts; it is refreshed automatically.
import {
  AuthenticationDetails, CognitoUser, CognitoUserPool, CognitoUserSession,
} from "amazon-cognito-identity-js";
import { getConfig } from "./config";

let pool: CognitoUserPool | null = null;
function userPool(): CognitoUserPool {
  const c = getConfig();
  pool ??= new CognitoUserPool({ UserPoolId: c.userPoolId, ClientId: c.clientId });
  return pool;
}

export type SignInResult = { kind: "ok" } | { kind: "new-password"; complete: (pw: string) => Promise<void> };

export function signIn(email: string, password: string): Promise<SignInResult> {
  const user = new CognitoUser({ Username: email.trim().toLowerCase(), Pool: userPool() });
  const details = new AuthenticationDetails({ Username: email.trim().toLowerCase(), Password: password });
  return new Promise((resolve, reject) => {
    user.authenticateUser(details, {
      onSuccess: () => resolve({ kind: "ok" }),
      onFailure: (err) => reject(friendly(err)),
      newPasswordRequired: () =>
        resolve({
          kind: "new-password",
          complete: (pw) =>
            new Promise((res, rej) =>
              user.completeNewPasswordChallenge(pw, {}, {
                onSuccess: () => res(),
                onFailure: (err) => rej(friendly(err)),
              })),
        }),
    });
  });
}

export function signOut(): void {
  if (getConfig().mock) return;
  userPool().getCurrentUser()?.signOut();
}

/** A valid ID token, refreshed if needed; null when not signed in. */
export function idToken(): Promise<string | null> {
  if (getConfig().mock) return Promise.resolve("mock-token");
  const user = userPool().getCurrentUser();
  if (!user) return Promise.resolve(null);
  return new Promise((resolve) =>
    user.getSession((err: Error | null, session: CognitoUserSession | null) =>
      resolve(!err && session?.isValid() ? session.getIdToken().getJwtToken() : null)));
}

function friendly(err: { code?: string; message?: string }): Error {
  const byCode: Record<string, string> = {
    NotAuthorizedException: "Incorrect email or password.",
    UserNotFoundException: "Incorrect email or password.",
    InvalidPasswordException: "Password must be at least 12 characters with upper and lower case letters and a number.",
    PasswordResetRequiredException: "A password reset is required for this account.",
  };
  return new Error((err.code && byCode[err.code]) || err.message || "Sign-in failed.");
}
