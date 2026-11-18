import { PublicClientApplication } from "@azure/msal-browser";

const env = import.meta.env;
const request = { scopes: [`api://${env.VITE_API_CLIENT_ID}/access_as_user`] };

const msal = new PublicClientApplication({
    auth: {
        clientId: env.VITE_WEB_CLIENT_ID,
        authority: `https://login.microsoftonline.com/${env.VITE_TENANT_ID}`,
        redirectUri: "/redirect",
        postLogoutRedirectUri: "/redirect",
    },
});

async function showMe(account) {
    const { accessToken } = await msal.acquireTokenSilent({ ...request, account });
    const response = await fetch("/me", { headers: { Authorization: `Bearer ${accessToken}` } });
    document.getElementById("me").textContent = await response.text();
}

await msal.initialize();
const redirected = await msal.handleRedirectPromise();
if (redirected) {
    await showMe(redirected.account);
}

document.getElementById("sign-in-popup").addEventListener("click", async () => {
    const { account } = await msal.loginPopup(request);
    await showMe(account);
});

document.getElementById("sign-in-redirect").addEventListener("click", () => {
    msal.loginRedirect(request);
});
