// Sign-in against the Cognito user pool (SRP: the password never leaves the
// browser in clear). Invited users first sign in with a temporary password
// and must choose a new one. A forgotten password is reset with a code that
// Cognito emails to the user. The ID token (which carries custom:tenant) is
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

/** Emails a reset code. Succeeds for unknown emails too (the pool doesn't reveal who has a login). */
export function forgotPassword(email: string): Promise<void> {
  if (getConfig().mock) return Promise.resolve();
  const user = new CognitoUser({ Username: email.trim().toLowerCase(), Pool: userPool() });
  return new Promise((resolve, reject) =>
    user.forgotPassword({
      onSuccess: () => resolve(),
      inputVerificationCode: () => resolve(),
      onFailure: (err) => reject(friendly(err, RESET_ERRORS)),
    }));
}

/** Sets a new password with the emailed code. */
export function resetPassword(email: string, code: string, password: string): Promise<void> {
  if (getConfig().mock) return Promise.resolve();
  const user = new CognitoUser({ Username: email.trim().toLowerCase(), Pool: userPool() });
  return new Promise((resolve, reject) =>
    user.confirmPassword(code.trim(), password, {
      onSuccess: () => resolve(),
      onFailure: (err) => reject(friendly(err, RESET_ERRORS)),
    }));
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

const PASSWORD_RULES = "Password must be at least 12 characters with upper and lower case letters and a number.";
const SIGN_IN_ERRORS: Record<string, string> = {
  NotAuthorizedException: "Incorrect email or password.",
  UserNotFoundException: "Incorrect email or password.",
  InvalidPasswordException: PASSWORD_RULES,
  PasswordResetRequiredException: "This account needs a new password: use “Forgot password?” below.",
};
const RESET_ERRORS: Record<string, string> = {
  CodeMismatchException: "That code is incorrect. Check the email and try again.",
  ExpiredCodeException: "That code has expired. Request a new one.",
  LimitExceededException: "Too many attempts. Please wait a few minutes and try again.",
  TooManyRequestsException: "Too many attempts. Please wait a few minutes and try again.",
  InvalidPasswordException: PASSWORD_RULES,
  NotAuthorizedException: "This password can't be reset yet. Sign in with the temporary password from your invitation, or ask your admin to invite you again.",
};

function friendly(err: { code?: string; message?: string }, byCode = SIGN_IN_ERRORS): Error {
  return new Error((err.code && byCode[err.code]) || err.message || "Something went wrong. Please try again.");
}
