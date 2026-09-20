// 검증 전용 실행 파일에서 제품의 동일 입력 검사와 바이너리 적재 함수를 재사용합니다.
#define wmain colmapWorkerEntryForTrace
#include "../../../tools/align/native/cccore_worker/main.cpp"
#undef wmain

// 초기 평가와 매 반복의 점 수·RMS·단계 변환·채택 여부를 파일에 즉시 기록합니다.
void observeIteration(void* payload, unsigned iteration, unsigned count, double rms,
                      const CCCoreLib::RegistrationTools::ScaledTransformation& step, bool accepted) {
    auto& stream = *static_cast<std::ofstream*>(payload);
    Transform transform;
    transform.R = step.R;
    transform.T = step.T;
    transform.s = step.s;
    stream << Json{{"iteration", iteration}, {"point_count", count}, {"rms", rms},
                   {"accepted", accepted}, {"step_matrix_4x4", matrixJson(transform)}}.dump() << '\n';
    stream.flush();
    require(bool(stream), "OUTPUT_IO_ERROR", "반복 관찰 파일을 기록하지 못했습니다.");
}

// 보존한 세션 명령의 입력을 기존 API·동적 격자·재사용 격자 중 한 경로로 실행합니다.
int wmain(int argc, wchar_t** argv) {
    configureHeadlessErrorReporting();
    Json result;
    try {
        require(argc == 5, "INVALID_ARGUMENT", "사용법: 명령JSON 작업루트 출력접두사 legacy|dynamic|cached");
        const fs::path requestPath(argv[1]), root(argv[2]), prefix(argv[3]);
        const std::wstring mode(argv[4]);
        require(mode == L"legacy" || mode == L"dynamic" || mode == L"cached", "INVALID_ARGUMENT", "검증 경로가 올바르지 않습니다.");
        std::ifstream requestStream(requestPath, std::ios::binary);
        auto request = Json::parse(requestStream);
        require(request.at("operation") == "register", "INVALID_OPERATION", "등록 명령만 검증할 수 있습니다.");
        const auto threads = resolveThreadSettings(request);
        configureScheduler(threads.effective);
        Progress progress(prefix.wstring() + L".progress.json", root / "cancel.request");
        progress.start();
        auto data = loadCloud(request.at("data"), root, true, progress);
        auto model = loadCloud(request.at("model"), root, true, progress);
        auto mesh = loadMesh(request.at("model"), root, *model, progress);
        require(bool(mesh), "INVALID_MESH", "삼각면 모델이 필요합니다.");
        auto params = parameters(request.at("parameters"), data->size(), model->size(), true, threads.effective, true);
        Transform transform;
        double rms = -1;
        unsigned count = 0;
        CCCoreLib::ICPMeshRegistrationContext context;
        context.reuseMeshGrid = mode == L"cached";
        std::ofstream trace(fs::path(prefix.wstring() + L".trace.jsonl"), std::ios::binary);
        context.observer = observeIteration;
        context.observerData = &trace;
        const auto started = Clock::now();
        const auto code = mode == L"legacy"
            ? CCCoreLib::ICPRegistrationTools::Register(model.get(), mesh.get(), data.get(), params, transform, rms, count, &progress)
            : CCCoreLib::ICPRegistrationTools::RegisterWithMeshContext(model.get(), mesh.get(), data.get(), params,
                transform, rms, count, context, &progress);
        result = {{"status", "completed"}, {"native_code", code}, {"final_rms", rms},
                  {"final_point_count", count}, {"matrix_4x4", matrixJson(transform)},
                  {"kernel_seconds", elapsedSeconds(started)},
                  {"icp_statistics", mode == L"legacy" ? Json(nullptr) : icpStatisticsJson(context.statistics)}};
        progress.check();
        require(code < CCCoreLib::ICPRegistrationTools::ICP_ERROR, "NATIVE_REGISTRATION_ERROR", "등록 검증 계산이 실패했습니다.");
        writeJson(prefix.wstring() + L".result.json", result);
        return 0;
    } catch (const std::exception& error) {
        result["status"] = "error";
        result["message"] = error.what();
        if (argc == 5) writeJson(fs::path(argv[3]).wstring() + L".result.json", result);
        std::cerr << result.dump() << std::endl;
        return 1;
    }
}
