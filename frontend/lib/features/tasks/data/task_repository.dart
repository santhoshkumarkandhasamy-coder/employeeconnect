import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../domain/task_models.dart';

/// api-contract.md section 15. Not cached: tasks change often and are small.
class TaskRepository {
  TaskRepository(this._dio);
  final Dio _dio;

  Future<List<TaskItem>> list({required String from, required String to, bool assignedByMe = false}) =>
      guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>('/tasks', queryParameters: {
          'from': from,
          'to': to,
          if (assignedByMe) 'assigned_by_me': true,
        },);
        return TaskItem.parseList(res.data ?? const <String, dynamic>{});
      });

  Future<TaskItem> create(Map<String, Object?> body) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>('/tasks', data: body);
        return TaskItem.fromJson(res.data!);
      });

  Future<TaskItem> update(String id, Map<String, Object?> body) => guardApi(() async {
        final res = await _dio.put<Map<String, dynamic>>('/tasks/$id', data: body);
        return TaskItem.fromJson(res.data!);
      });

  Future<TaskItem> setDone(String id, {required bool done}) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>('/tasks/$id/done', data: {'done': done});
        return TaskItem.fromJson(res.data!);
      });

  Future<void> delete(String id) => guardApi(() async {
        await _dio.delete<void>('/tasks/$id');
      });
}

final taskRepositoryProvider = Provider<TaskRepository>((ref) => TaskRepository(ref.watch(dioProvider)));
