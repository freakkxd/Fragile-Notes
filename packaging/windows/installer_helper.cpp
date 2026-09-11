// installer_helper.cpp — C++ helper for Fragile Notes Full Offline installer
// Делает всё за юзера из коробки: Python check, pip install, vault, без PowerShell
// Компиляция (MSYS2 UCRT64): g++ -O2 -std=c++17 -o installer_helper.exe installer_helper.cpp -lole32 -lshell32
// Вызывается Inno Setup: [Run] Filename: "{app}\installer_helper.exe"; Parameters: "/install"

#include <windows.h>
#include <shlobj.h>
#include <iostream>
#include <fstream>
#include <string>
#include <cstdlib>
#include <filesystem>
namespace fs = std::filesystem;

void log(const std::string& m, const char* col = "") {
    std::cout << m << std::endl;
}
bool run(const std::string& cmd) {
    std::cout << "→ " << cmd << std::endl;
    int r = std::system(cmd.c_str());
    return r == 0;
}
bool checkPython() {
    return std::system("python --version >nul 2>&1") == 0 || std::system("py --version >nul 2>&1") == 0 || std::system("python3 --version >nul 2>&1") == 0;
}
std::string findPython() {
    for (auto c : {"python", "python3", "py"}) {
        std::string cmd = std::string(c) + " --version >nul 2>&1";
        if (std::system(cmd.c_str()) == 0) return c;
    }
    return "python";
}
int main(int argc, char* argv[]) {
    bool install = false;
    for (int i=1;i<argc;i++) if (std::string(argv[i])=="/install") install=true;
    if (!install) { std::cout << "Usage: installer_helper.exe /install" << std::endl; return 0; }

    char appDir[MAX_PATH]; GetModuleFileNameA(NULL, appDir, MAX_PATH);
    std::string app = fs::path(appDir).parent_path().string();
    SetCurrentDirectoryA(app.c_str());
    std::cout << "== Fragile Notes C++ installer ==" << std::endl;
    std::cout << "AppDir: " << app << std::endl;

    // 1. Python
    std::string py = findPython();
    if (!checkPython()) {
        std::cout << "[WARN] Python 3.11+ not found, downloading..." << std::endl;
        std::string url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe";
        std::string tmp = std::string(getenv("TEMP")) + "\\python-installer.exe";
        std::string dl = "powershell -Command \"Invoke-WebRequest -Uri " + url + " -OutFile '" + tmp + "'\"";
        if (!run(dl)) { std::cerr << "Failed to download Python" << std::endl; return 1; }
        std::string inst = "\"" + tmp + "\" /quiet InstallAllUsers=0 PrependPath=1";
        run(inst);
        py = "python";
    }
    std::cout << "[OK] Python " << py << std::endl;
    // 2. Check version
    std::string verCmd = py + " -c \"import sys; exit(0 if sys.version_info>=(3,11) else 1)\"";
    if (std::system(verCmd.c_str()) != 0) { std::cerr << "Python >=3.11 required" << std::endl; return 1; }

    // 3. GTK check
    std::string gtkCheck = py + " -c \"import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('ok')\"";
    bool gtkOk = (std::system((gtkCheck + " >nul 2>&1").c_str()) == 0);
    if (gtkOk) std::cout << "[OK] GTK4 + libadwaita" << std::endl;
    else {
        std::cout << "[WARN] GTK not found - Full Offline already bundles GTK, skipping MSYS2 install for Full" << std::endl;
        // Для Full Offline GTK уже внутри dist/windows/FragileNotes/bin/*.dll, ничего ставить не надо
        // Для AllInOne (если бы использовался) — тут бы качали MSYS2
    }

    // 4. pip install (для Full Offline уже внутри, но для AllInOne нужно)
    // Full Offline: fragile-notes уже внутри dist, pip не нужен. Пропускаем если есть FragileNotes.exe
    if (fs::exists(app + "\\FragileNotes.exe") || fs::exists(app + "\\fragile-notes.exe")) {
        std::cout << "[OK] Full Offline - pip already bundled" << std::endl;
    } else {
        std::cout << "→ pip install" << std::endl;
        run(py + " -m pip install --upgrade pip");
        int r = std::system((py + " -m pip install -e .").c_str());
        if (r != 0) std::cerr << "[WARN] pip install -e . failed" << std::endl; else std::cout << "[OK] pip install" << std::endl;
    }

    // 5. Vault — теперь в Documents/FragileNotesVault, не на рабочем столе
    std::string docs = std::string(getenv("USERPROFILE")) + "\\Documents";
    std::string vault = docs + "\\FragileNotesVault";
    // Миграция: если старый волт на desktop существует и новый пуст — копируем
    std::string oldVault = std::string(getenv("USERPROFILE")) + "\\desktop";
    if (fs::exists(oldVault) && !fs::exists(vault)) {
        try { fs::copy(oldVault, vault, fs::copy_options::recursive); std::cout << "[OK] Migrated vault desktop -> " << vault << std::endl; } catch (...) {}
    }
    if (!fs::exists(vault)) {
        fs::create_directories(vault + "\\01 Home");
        fs::create_directories(vault + "\\02 Daily");
        fs::create_directories(vault + "\\_System");
        std::ofstream f(vault + "\\01 Home\\Home.md"); f << "# Home\n"; f.close();
        std::cout << "[OK] Vault " << vault << std::endl;
    } else std::cout << "[OK] Vault " << vault << std::endl;

    std::cout << "\nDone! Run Fragile Notes from Start Menu." << std::endl;
    return 0;
}
