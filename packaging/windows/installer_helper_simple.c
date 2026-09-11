// installer_helper_simple.c — Minimal C helper, no libstdc++ needed
// Compile with: gcc -O2 -o installer_helper.exe installer_helper_simple.c -lole32 -lshell32
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char* argv[]) {
    int do_install = 0;
    for (int i=1; i<argc; i++) if (strcmp(argv[i], "/install")==0) do_install=1;
    if (!do_install) { printf("Usage: installer_helper.exe /install\n"); return 0; }

    char appDir[MAX_PATH];
    GetModuleFileNameA(NULL, appDir, MAX_PATH);
    char* p = strrchr(appDir, '\\');
    if (p) *p = '\0';
    SetCurrentDirectoryA(appDir);
    printf("== Fragile Notes C installer ==\nAppDir: %s\n", appDir);

    // Vault
    char vault[MAX_PATH];
    snprintf(vault, sizeof(vault), "%s\\desktop", getenv("USERPROFILE"));
    DWORD attr = GetFileAttributesA(vault);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        char path[MAX_PATH];
        snprintf(path, sizeof(path), "%s\\01 Home", vault);
        CreateDirectoryA(path, NULL);
        snprintf(path, sizeof(path), "%s\\02 Daily", vault);
        CreateDirectoryA(path, NULL);
        snprintf(path, sizeof(path), "%s\\_System", vault);
        CreateDirectoryA(path, NULL);
        snprintf(path, sizeof(path), "%s\\01 Home\\Home.md", vault);
        FILE* f = fopen(path, "w");
        if (f) { fprintf(f, "# Home\n"); fclose(f); }
        printf("[OK] Vault %s\n", vault);
    } else printf("[OK] Vault %s\n", vault);

    printf("\nDone! Run Fragile Notes from Start Menu.\n");
    return 0;
}
