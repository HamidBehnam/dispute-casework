import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
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
});
