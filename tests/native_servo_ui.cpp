// Bounded Qt regression check against test_native_servo_ui.py's fake HTTP server.
#define main shore_desktop_main
#include "../native/main.cpp"
#undef main

int main(int argc,char **argv) {
    QApplication app(argc,argv);
    if(webAddress("192.0.2.10",80)!="192.0.2.10"
       || webAddress("192.0.2.10",8090)!="192.0.2.10:8090"
       || webAddress("192.0.2.10",18091)!="192.0.2.10:18091")qFatal("Web address must hide only port 80");
    if(argc>1 && QString::fromLocal8Bit(argv[1])=="--address-only")return 0;
    Desktop desktop(QUrl(QString::fromLocal8Bit(argv[1])),false);
    desktop.show();desktop.resize(1024,600);QApplication::processEvents();
    auto named=[&](const QString &name)->QPushButton* {
        for(auto b:desktop.findChildren<QPushButton*>())if(b->accessibleName()==name)return b;
        qFatal("Window control missing");return nullptr;
    };
    auto settings=named("設定"),minimise=named("縮小或還原視窗"),exit=named("關閉視窗");
    if(desktop.windowTitle()!="Shore Jetson")qFatal("Application title is not Shore Jetson");
    for(auto b:desktop.findChildren<QPushButton*>())
        if(b->text().contains("重新連接相機"))qFatal("Manual camera reconnect button must be removed");
    if(settings->x()!=minimise->x() || settings->x()!=exit->x()
       || !(exit->y()<minimise->y() && minimise->y()<settings->y())
       || settings->y()>200 || settings->size()!=QSize(50,50))qFatal("Window controls are not together at the top right");
    desktop.showDrawer();
    if(settings->isVisible() || minimise->isVisible() || exit->isVisible())qFatal("Window controls did not hide with settings");
    auto button=desktop.findChild<QPushButton*>("servoTestButton");
    auto notice=desktop.findChild<QLabel*>("servoTestState");
    auto recovery=desktop.findChild<QLabel*>("recoveryState");
    QTimer::singleShot(1000,[=]{if(!button->isEnabled())qFatal("Test button unavailable");button->click();});
    QTimer::singleShot(1400,[=]{
        if(!button->text().contains("\n停止測試") || !button->isEnabled() || notice->isVisible())qFatal("Inline progress/STOP unavailable");
        if(button->text()!="抬頭 40°\n停止測試")qFatal("Fourfold test angle missing from native button");
    });
    QTimer::singleShot(2900,[=]{
        if(notice->isVisible() || button->text()!="雲台測試" || !recovery->text().isEmpty())qFatal("Completion/recovery text did not clear");
        button->click();
    });
    QTimer::singleShot(2950,[&]{QEvent event(QEvent::WindowDeactivate);QApplication::sendEvent(&desktop,&event);});
    QTimer::singleShot(4300,[=]{if(button->text()!="雲台測試")qFatal("Late start survived cancellation");button->click();});
    QTimer::singleShot(5400,[=]{
        if(!notice->isVisible() || !notice->text().contains("請先結束錄影"))qFatal("Backend rejection was lost");
        button->click();
    });
    QTimer::singleShot(5900,[=]{
        if(!button->text().contains("\n停止測試") || !button->isEnabled() || notice->isVisible())qFatal("Retry did not clear error/enable STOP");
        button->click();
    });
    QTimer::singleShot(6500,[&]{if(button->text()!="雲台測試")qFatal("STOP button did not stop test");app.quit();});
    return app.exec();
}
