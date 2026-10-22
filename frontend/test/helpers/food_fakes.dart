import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/food/domain/food_models.dart';

class FakeFoodRepo extends FoodRepository {
  FakeFoodRepo(this.days) : super(Dio(), MemoryJsonCache());

  List<FoodDay> days;
  ApiException? loadError;
  ApiException? setError;
  Set<String> bulkFail = {};
  FoodDailyReport report = const FoodDailyReport(date: '2026-10-21');
  ApiException? reportError;
  final List<(String, bool)> sets = [];
  final List<List<String>> bulks = [];

  @override
  Future<Cached<List<FoodDay>>> myEnrollments() async {
    final e = loadError;
    if (e != null) throw e;
    return Cached(days);
  }

  @override
  Future<void> remember(List<FoodDay> d) async {}

  @override
  Future<FoodDay> setOne(String date, {required bool needFood}) async {
    sets.add((date, needFood));
    await Future<void>.delayed(const Duration(milliseconds: 10));
    final e = setError;
    if (e != null) throw e;
    return FoodDay(date: date, needFood: needFood);
  }

  @override
  Future<FoodBulkResult> setMany(List<String> dates, {required bool needFood}) async {
    bulks.add(dates);
    return FoodBulkResult(
      items: [for (final d in dates) FoodDay(date: d, needFood: needFood)],
      failed: [for (final d in bulkFail) FoodBulkFailure(d, 'FOOD_CUTOFF_PASSED')],
    );
  }

  @override
  Future<FoodDailyReport> daily(String date) async {
    final e = reportError;
    if (e != null) throw e;
    return report;
  }
}
