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
    // A blocked diagnostics sink must not turn an ordinary pose log into a
    // render-thread storage wait. Publish while another handle denies opens,
    // then verify the worker retains the newest records when it can write.
    const auto logPath=root/(L"openxr_simulator."+std::to_wstring(GetCurrentProcessId())+L".log");
    HANDLE lockedLog=CreateFileW(logPath.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,
        CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
    CHECK(lockedLog!=INVALID_HANDLE_VALUE);
    const auto publishStart=GetTickCount64();
    for (unsigned i=0;i<2000;++i)
        flicker::PublishRuntimeLog("blocked-sink record "+std::to_string(i));
    flicker::PublishRuntimeLog("latest runtime diagnostic");
    const bool publicationBounded=GetTickCount64()-publishStart<1000;
    CloseHandle(lockedLog);
    CHECK(publicationBounded);
    bool logDelivered=false;
    const auto logUntil=GetTickCount64()+8000;
    do {
        std::ifstream file(logPath,std::ios::binary);
        const std::string contents((std::istreambuf_iterator<char>(file)),{});
        if (contents.find("latest runtime diagnostic")!=std::string::npos) {logDelivered=true;break;}
        Sleep(10);
    } while (GetTickCount64()<logUntil);
    CHECK(logDelivered);
    const auto resultPath=root/L"ui_pointer_result.once.json";
    HANDLE lockedResult=CreateFileW(resultPath.c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ,
        nullptr,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
    CHECK(lockedResult!=INVALID_HANDLE_VALUE);
    const auto resultStart=GetTickCount64();
    flicker::PublishRuntimeResult("ui_pointer_result.once.json","{\"success\":true}");
    const bool resultNonblocking=GetTickCount64()-resultStart<1000;
    Sleep(100); // Deny the worker's first atomic replacement attempt.
    CloseHandle(lockedResult);
    CHECK(resultNonblocking);
    bool resultDelivered=false;
    const auto resultUntil=GetTickCount64()+8000;
    do {
        std::ifstream file(resultPath,std::ios::binary);
        const std::string contents((std::istreambuf_iterator<char>(file)),{});
        file.close();
        if (contents=="{\"success\":true}") {resultDelivered=true;break;}
        Sleep(10);
    } while (GetTickCount64()<resultUntil);
    CHECK(resultDelivered);
    write("ui_pointer_command.json","{\"command_id\":\"once\"}");
    const auto replayUntil=GetTickCount64()+8000;
    while (std::filesystem::exists(root/"ui_pointer_command.json") && GetTickCount64()<replayUntil)Sleep(10);
    CHECK(!std::filesystem::exists(root/"ui_pointer_command.json"));
    CHECK(!flicker::TakeRuntimeCommand("ui_pointer_command.json",value));
    // No frame-side wakeup: the worker owns command-directory discovery.
    write("ui_pointer_command.json", "{\"command_id\":\"second\"}");
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
