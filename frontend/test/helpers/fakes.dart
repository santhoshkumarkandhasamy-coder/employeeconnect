import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/security/biometric_gate.dart';
import 'package:employee_connect/core/server/company.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:employee_connect/features/auth/domain/auth_user.dart';

class MemoryTokenStore extends TokenStore {
  String? access;
  String? refresh;
  bool biometric = false;

  @override
  Future<String?> readAccess() async => access;
  @override
  Future<String?> readRefresh() async => refresh;
  @override
  Future<void> saveTokens({required String access, required String refresh}) async {
    this.access = access;
    this.refresh = refresh;
  }

  @override
  Future<bool> readBiometricEnabled() async => biometric;
  @override
  Future<void> writeBiometricEnabled({required bool enabled}) async =>
      biometric = enabled;
  @override
  Future<String> deviceId() async => 'test-device';
  @override
  Future<void> clearSession() async {
    access = null;
    refresh = null;
    biometric = false;
  }
}

class FakeBiometricGate implements BiometricGate {
  FakeBiometricGate({this.result = true});
  bool result;
  @override
  Future<bool> isAvailable() async => true;
  @override
  Future<bool> authenticate(String reason) async => result;
}

const testUser = AuthUser(
  id: 'u1',
  employeeId: 'EMP001',
  fullName: 'Asha Rao',
  role: 'employee',
  permissions: [],
  mustChangePassword: false,
);

class FakeAuthRepository implements AuthRepository {
  ApiException? loginError;
  ApiException? meError;
  AuthUser loginUser = testUser;
  final List<String> loginCalls = [];
  bool loggedOut = false;

  @override
  Future<AuthUser> login(String employeeId, String password) async {
    loginCalls.add('$employeeId:$password');
    final e = loginError;
    if (e != null) throw e;
    return loginUser;
  }

  @override
  Future<AuthUser> me() async {
    final e = meError;
    if (e != null) throw e;
    return testUser;
  }
  @override
  Future<AuthUser> changePassword(String current, String next) async => testUser;
  @override
  Future<void> logout({bool all = false}) async => loggedOut = true;
  @override
  Future<AppConfig?> config() async => null;
  @override
  Future<CompanyInfo?> publicCompany() async => null;
}

/// Scriptable HTTP adapter for Dio tests.
class ScriptedAdapter implements HttpClientAdapter {
  ScriptedAdapter(this.handler);
  final ({int status, Object body}) Function(RequestOptions) handler;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final r = handler(options);
    return ResponseBody.fromString(
      jsonEncode(r.body),
      r.status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
