import argparse

def get_options():

    parser = argparse.ArgumentParser()
    parser.add_argument('--hfai_mode', action='store_true')
    parser.add_argument('--alg', default=None, help='naive, pcgrad, nashmtl, banditmtl, uw'
                                                               )
    parser.add_argument('--method_params_lr', type=float, default=0.025)

    # NashMTL
    parser.add_argument(
        "--nashmtl_optim_niter", type=int, default=20, help="number of CCCP iterations"
    )
    parser.add_argument(
        "--update_weights_every",
        type=int,
        default=1,
        help="update task weights every x iterations.",
    )
    # stl
    parser.add_argument(
        "--main-task",
        type=int,
        default=0,
        help="main task for stl. Ignored if method != stl",
    )

    # cagrad
    parser.add_argument("--c", type=float, default=0.4, help="c for CAGrad alg.")
    # dwa
    # dwa
    parser.add_argument(
        "--dwa_temp",
        type=float,
        default=2.0,
        help="Temperature hyper-parameter for DWA. Default to 2 like in the original paper.",
    )
    # banditmtl
    parser.add_argument('--rho', type=float, default=1.2)
    parser.add_argument('--eta_p', type=float, default=.5)

    # problem setting
    # seen tasks
    parser.add_argument('--tsp', nargs='+', type=int, default=None)
    parser.add_argument('--cvrp', nargs='+', type=int, default=None)
    parser.add_argument('--op', nargs='+', type=int, default=None)
    parser.add_argument('--kp', nargs='+', type=int, default=None)
    # unseen tasks
    parser.add_argument('--unseen_tsp', nargs='+', type=int, default=None)
    parser.add_argument('--unseen_cvrp', nargs='+', type=int, default=None)
    parser.add_argument('--unseen_op', nargs='+', type=int, default=None)
    parser.add_argument('--unseen_kp', nargs='+', type=int, default=None)
    # training mode
    parser.add_argument('--coord_same', action='store_true')
    parser.add_argument('--separate_train',type=bool, default=False)
    parser.add_argument('--rew_alpha', type=float, default=.5)

    # training params
    parser.add_argument('--epochs', type=int, default=1000)
    parser.add_argument('--train_episodes', type=int, default=100*1000)
    parser.add_argument('--train_batch_size', type=int, default=512)
    parser.add_argument('--patience', type=int, default=100)
    parser.add_argument('--factor', type=float, default=.5)
    parser.add_argument('--min_lr', type=float, default=1e-8)

    parser.add_argument('--evaluation_size', type=int, default=512)
    parser.add_argument('--model_save_interval', type=int, default=50)
    parser.add_argument('--model_load', action='store_true')
    parser.add_argument('--resume_path', type=str, default=None)
    parser.add_argument('--resume_epoch', type=int, default=None)

    parser.add_argument('--task_description', type=str, default=None)

    opts = parser.parse_args()
    return opts