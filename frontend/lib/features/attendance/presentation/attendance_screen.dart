import 'package:flutter/material.dart';

import '../../devices/presentation/my_punches.dart';
import 'calendar_tab.dart';
import 'history_tab.dart';
import 'today_tab.dart';

/// Attendance tab: Today (check in/out) | Calendar | History.
class AttendanceScreen extends StatelessWidget {
  const AttendanceScreen({super.key});

  @override
  Widget build(BuildContext context) => DefaultTabController(
        length: 4,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Attendance'),
            bottom: const TabBar(
              isScrollable: true,
              tabAlignment: TabAlignment.start,
              tabs: [
                Tab(text: 'Today'),
                Tab(text: 'Calendar'),
                Tab(text: 'History'),
                Tab(text: 'My punches'),
              ],
            ),
          ),
          body: const TabBarView(children: [TodayTab(), CalendarTab(), HistoryTab(), MyPunchesTab()]),
        ),
      );
}
