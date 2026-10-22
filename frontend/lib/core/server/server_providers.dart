import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/api_exception.dart';
import '../api/dio_client.dart';
import '../storage/secure_storage.dart';
import 'company.dart';
import 'server_url.dart';

/// Effective API base URL (null = Server setup is needed). `main` overrides
/// the initial value with the saved server URL.
final baseUrlProvider = StateProvider<String?>((ref) => resolveBaseUrl());

/// True when the app has no usable server URL yet (first run).
final serverSetupRequiredProvider = StateProvider<bool>((ref) => false);

/// Calls `GET /public/company` on a candidate base URL through a freshly
/// built (pinned) client. Overridable in tests.
typedef ServerProbe = Future<ServerProbeResult> Function(String baseUrl);

class ServerProbeResult {
  const ServerProbeResult(this.company, [this.logo]);
  final CompanyInfo company;
  final Uint8List? logo;
}

final serverProbeProvider = Provider<ServerProbe>((ref) {
  final store = ref.watch(tokenStoreProvider);
  final events = ref.watch(sessionEventsProvider);
  return (baseUrl) async {
    try {
      final dio = createDio(store: store, events: events, baseUrl: baseUrl);
      final res = await dio.get<Object?>(
        '/public/company',
        options: Options(extra: const {noAuthKey: true}),
      );
      final c = CompanyInfo.parse(res.data);
      if (c == null || !c.isEmployeeConnect) {
        throw const ApiException(
          code: 'NOT_EMPLOYEE_CONNECT',
          message: 'That address is not an Employee Connect server.',
        );
      }
      Uint8List? logo;
      if (c.logoUrl != null) {
        try {
          final r = await dio.get<List<int>>(
            '/public/company/logo',
            queryParameters: {'v': c.logoVersion},
            options: Options(responseType: ResponseType.bytes, extra: const {noAuthKey: true}),
          );
          if (r.data != null && r.data!.isNotEmpty) logo = Uint8List.fromList(r.data!);
        } on DioException {
          logo = null;
        }
      }
      return ServerProbeResult(c, logo);
    } on DioException catch (e) {
      throw ApiException.fromDio(e);
    } on TypeError {
      throw const ApiException(
        code: 'NOT_EMPLOYEE_CONNECT',
        message: 'That address is not an Employee Connect server.',
      );
    } on StateError catch (e) {
      throw ApiException(code: 'INSECURE_SERVER', message: e.message);
    }
  };
});
