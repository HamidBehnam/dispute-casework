import { PublicClientApplication } from "@azure/msal-browser";

const env = import.meta.env;
const request = { scopes: [`api://${env.VITE_API_CLIENT_ID}/access_as_user`] };

const msal = new PublicClientApplication({
    auth: {
        clientId: env.VITE_WEB_CLIENT_ID,
        authority: `https://login.microsoftonline.com/${env.VITE_TENANT_ID}`,
        redirectUri: "/redirect",
    },
});

function showError({ errorCode, subError, errorNo }) {
    document.getElementById("error").textContent = JSON.stringify({ errorCode, subError, errorNo });
}

async function showMe(account) {
    const { accessToken } = await msal.acquireTokenSilent({ ...request, account });
    const response = await fetch("/me", { headers: { Authorization: `Bearer ${accessToken}` } });
    if (!response.ok) {
        document.getElementById("error").textContent = JSON.stringify({ status: response.status });
        return;
    }
    document.getElementById("me").textContent = await response.text();
}

try {
    await msal.initialize();
    const redirected = await msal.handleRedirectPromise();
    if (redirected) {
        await showMe(redirected.account);
    }
} catch (error) {
    showError(error);
}

document.getElementById("sign-in-popup").addEventListener("click", async () => {
    try {
        const { account } = await msal.loginPopup(request);
        await showMe(account);
    } catch (error) {
        showError(error);
    }
});

document.getElementById("sign-in-redirect").addEventListener("click", () => {
    msal.loginRedirect(request).catch(showError);
});
