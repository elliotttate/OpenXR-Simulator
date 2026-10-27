#include "flicker_detector.h"
#include <windows.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#define CHECK(value) do { if (!(value)) { std::cerr << "Failed: " #value << '\n'; return 1; } } while (false)

int main() {
    wchar_t temp[MAX_PATH] = {};
    CHECK(GetTempPathW(MAX_PATH, temp));
    const auto root = std::filesystem::path(temp) /
        (L"simxr-command-worker-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
    std::filesystem::create_directories(root);
    CHECK(SetEnvironmentVariableW(L"SIMXR_DATA_DIR", root.c_str()));
    const auto write = [&](const char* name, const char* value) {
        std::ofstream file(root / name, std::ios::binary);
        file << value;
    };
    const auto take = [](const char* name, std::string& value) {
        const auto until = GetTickCount64() + 8000;
        do {
            if (flicker::TakeRuntimeCommand(name, value)) return true;
            Sleep(10);
        } while (GetTickCount64() < until);
        return false;
    };
    write("ui_pointer_command.json", "{\"command_id\":\"first\"}");
    write("head_pose_command.json", "{\"yaw\":0.4}");
    write("untouched.json", "preserve");
    flicker::RequestRuntimeCommands();
    std::string value;
    CHECK(take("ui_pointer_command.json", value) && value == "{\"command_id\":\"first\"}");
    CHECK(!std::filesystem::exists(root / "ui_pointer_command.json"));
    CHECK(!flicker::TakeRuntimeCommand("ui_pointer_command.json", value));
    CHECK(take("head_pose_command.json", value) && value == "{\"yaw\":0.4}");
    CHECK(std::filesystem::exists(root / "untouched.json"));
    write("ui_pointer_command.json", "{\"command_id\":\"second\"}");
    flicker::RequestRuntimeCommands();
    CHECK(take("ui_pointer_command.json", value) && value == "{\"command_id\":\"second\"}");
    for (int i = 0; i < 1000; ++i)
        flicker::PublishRuntimeStatus("{\"frame_count\":" + std::to_string(i) + "}");
    const auto until = GetTickCount64() + 8000;
    do {
        std::ifstream file(root / "runtime_status.json");
        std::string status((std::istreambuf_iterator<char>(file)), {});
        file.close();
        if (status == "{\"frame_count\":999}") {
            HANDLE reader = CreateFileW((root / "runtime_status.json").c_str(), GENERIC_READ,
                FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
            CHECK(reader != INVALID_HANDLE_VALUE);
            flicker::PublishRuntimeStatus("{\"frame_count\":1000}");
            Sleep(350); // Deny replacement while the background writer tries.
            flicker::PublishRuntimeStatus("{\"frame_count\":1001}");
            Sleep(350);
            CloseHandle(reader);
            const auto retryUntil = GetTickCount64() + 8000;
            do {
                std::ifstream retryFile(root / "runtime_status.json");
                std::string latest((std::istreambuf_iterator<char>(retryFile)), {});
                retryFile.close();
                if (latest == "{\"frame_count\":1001}") {
                    std::cout << "Command delivery, isolation, latest status and blocked-reader recovery passed\n";
                    return 0;
                }
                Sleep(10);
            } while (GetTickCount64() < retryUntil);
            CHECK(false);
        }
        Sleep(10);
    } while (GetTickCount64() < until);
    CHECK(false);
}
