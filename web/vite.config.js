import { resolve } from "node:path";
import { defineConfig, loadEnv } from "vite";

const REQUIRED = ["VITE_TENANT_ID", "VITE_WEB_CLIENT_ID", "VITE_API_CLIENT_ID"];

export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, import.meta.dirname);
    const missing = REQUIRED.filter((name) => !env[name]);
    if (missing.length > 0) {
        throw new Error(`Missing or empty build values: ${missing.join(", ")}`);
    }
    return {
        build: {
            outDir: resolve(import.meta.dirname, "../src/dispute_casework/static"),
            emptyOutDir: true,
            rollupOptions: {
                input: {
                    main: resolve(import.meta.dirname, "index.html"),
                    redirect: resolve(import.meta.dirname, "redirect.html"),
                },
            },
        },
    };
});
