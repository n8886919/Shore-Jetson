// In-process Qt regression against a fake HTTP backend; no camera or UART.
#define main shore_desktop_main
#include "../native/main.cpp"
#undef main

int main(int argc, char **argv) {
    QApplication app(argc,argv);
    if (argc != 3) return 2;
    Desktop desktop(QUrl(argv[1]),false);
    desktop.show(); desktop.showDrawer();
    auto toggle=desktop.findChild<QCheckBox*>("inferenceEnabled");
    if (!toggle) return 3;
    auto require=[](bool condition,const char *message){if(!condition)qFatal("%s",message);};
    auto lock=[=](bool value){QFile f(argv[2]);if(!f.open(QIODevice::WriteOnly|QIODevice::Truncate))qFatal("fixture file");f.write(value?"locked":"unlocked");};
    QTimer::singleShot(800,[=]{require(toggle->isEnabled()&&!toggle->isChecked(),"AI must default off");toggle->click();});
    QTimer::singleShot(1600,[=]{require(toggle->isChecked(),"AI enable readback");lock(true);});
    QTimer::singleShot(2400,[=]{require(toggle->isChecked()&&!toggle->isEnabled(),"AI locked during recording");toggle->click();lock(false);});
    QTimer::singleShot(3200,[=]{require(toggle->isEnabled()&&toggle->isChecked(),"AI unlock after recording");toggle->click();});
    QTimer::singleShot(4000,[&]{require(!toggle->isChecked(),"AI disable readback");app.quit();});
    return app.exec();
}
