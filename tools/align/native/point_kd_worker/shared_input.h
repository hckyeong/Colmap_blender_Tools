#pragma once
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#ifdef _WIN32
#include <windows.h>
#endif

namespace point_kd {
// 작업별 공유 입력을 읽기 전용으로 열고 범위·표식·수명을 함께 보호합니다.
class SharedInput {
public:
    // 전달받은 크기만 매핑하며 헤더가 작업 이름과 일치해야 데이터를 공개합니다.
    SharedInput(const std::string& name, std::size_t bytes) {
        const std::string prefix = "Local\\ColmapPointKd_";
        if (name.size() != prefix.size() + 32 || name.compare(0, prefix.size(), prefix) != 0 ||
            name.find_first_not_of("0123456789abcdef", prefix.size()) != std::string::npos ||
            bytes == 0 || bytes > std::numeric_limits<std::size_t>::max() - 64)
            throw std::runtime_error("공유 입력의 이름 또는 크기가 잘못되었습니다.");
#ifdef _WIN32
        const std::wstring wide(name.begin(), name.end());
        handle_ = OpenFileMappingW(FILE_MAP_READ, FALSE, wide.c_str());
        if (!handle_) throw std::runtime_error("공유 입력을 열지 못했습니다.");
        view_ = static_cast<const char*>(MapViewOfFile(handle_, FILE_MAP_READ, 0, 0, bytes + 64));
        if (!view_) { release(); throw std::runtime_error("공유 입력의 요청 범위를 매핑하지 못했습니다."); }
        std::uint64_t declared = 0;
        std::memcpy(&declared, view_ + 8, sizeof(declared));
        const char reserved[16] = {};
        if (std::memcmp(view_, "CPKDIPC1", 8) || declared != bytes ||
            std::memcmp(view_ + 16, name.data() + prefix.size(), 32) || std::memcmp(view_ + 48, reserved, 16)) {
            release(); throw std::runtime_error("공유 입력의 표식·크기·작업 이름이 일치하지 않습니다.");
        }
#else
        throw std::runtime_error("공유 입력은 Windows에서만 지원합니다.");
#endif
    }
    SharedInput(const SharedInput&) = delete;
    SharedInput& operator=(const SharedInput&) = delete;
    // 정상 종료와 예외 모두 읽기 전용 뷰와 Windows 핸들을 해제합니다.
    ~SharedInput() { release(); }
    // 검증한 헤더 다음의 원본 바이트를 소유 버퍼 복사에 제공합니다.
    const char* data() const { return view_ + 64; }
private:
    const char* view_ = nullptr;
#ifdef _WIN32
    HANDLE handle_ = nullptr;
#endif
    // 생성 중 실패한 경우에도 이미 열린 자원을 남기지 않습니다.
    void release() noexcept {
#ifdef _WIN32
        if (view_) { UnmapViewOfFile(view_); view_ = nullptr; }
        if (handle_) { CloseHandle(handle_); handle_ = nullptr; }
#endif
    }
};
}
